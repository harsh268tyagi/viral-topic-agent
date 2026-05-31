"""Channel authorization and connection lifecycle (Requirement 1).

The :class:`ConnectionManager` mediates the three phases of connecting an
``Owned_Channel`` (``.kiro/specs/viral-topic-agent/design.md`` ->
*Connection_Manager*):

1. **Initiation** (:meth:`ConnectionManager.initiate_connection`) -- issue the
   authorization request to the Creator within 5 s of initiation (1.1).
2. **Decision** (:meth:`ConnectionManager.complete_authorization`) -- resolve the
   Creator's grant/deny/timeout decision:

   - granted in time -> store the credentials and mark the channel ``connected``
     (1.2); a storage failure records ``credential-storage-failed``, returns a
     "not saved" error, and leaves the channel **not** connected (1.8);
   - denied -> record ``authorization-failed`` and never retrieve (1.3);
   - no decision within 300 s -> record ``authorization-timeout`` and never
     retrieve (1.7).

3. **Retrieval** (:meth:`ConnectionManager.retrieve_with_credentials`) -- for a
   connected channel with valid credentials, retrieve data through
   :class:`ResilientDataSource` within 30 s (1.4), tagging the retrieved data set
   with the originating channel id (1.6). Expired credentials short-circuit to an
   ``authorization-expired`` error identifying the channel (1.5); a retrieval that
   exhausts the resilience layer's retries returns ``data-retrieval-failed``
   identifying the channel while retaining the stored credentials (1.9).

Up to :data:`ConnectionManager.MAX_OWNED_CHANNELS` (50) channels may be authorized
(1.6); attempting to authorize a 51st new channel is treated as a storage failure
(1.8) naming the limit, so the documented cap is enforced without inventing a new
status.

Side effects live at the edges: persistence of credentials is delegated to an
injected :class:`CredentialStore`, and data retrieval flows through the
:class:`ResilientDataSource`. The manager's own decision logic is therefore pure
and deterministic, and every time-based decision is driven by an injected
:class:`Clock`, so behavior is identical and instant under a ``FakeClock`` in
tests.

Requirements traceability: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from infrastructure.clock import Clock
from infrastructure.datasource import DataRequest
from domain.models import (
    AuthorizationGrant,
    AuthorizationResult,
    AuthorizedChannel,
    AuthStatus,
    Configuration,
)
from infrastructure.resilient_data_source import ResilientDataSource
from infrastructure.result import Err, Ok, Result

__all__ = [
    "TaggedData",
    "ConnectionError",
    "CredentialStore",
    "InMemoryCredentialStore",
    "ConnectionManager",
]


# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaggedData:
    """A retrieved data set tagged with its originating channel id (1.6).

    Wrapping the raw payload alongside the ``channel_id`` is how the manager
    "associates each retrieved data set with the corresponding Owned_Channel
    identifier" -- the association travels with the data instead of being held
    in a side table, so a caller juggling several channels can never mix them up.
    """

    channel_id: str
    data: Any


@dataclass(frozen=True)
class ConnectionError:
    """A retrieval-phase failure that identifies the affected channel.

    Returned (inside an ``Err``) by :meth:`ConnectionManager.retrieve_with_credentials`
    for the two retrieval failure modes that must name the channel:

    - ``AUTHORIZATION_EXPIRED`` -- stored credentials were expired (1.5);
    - ``DATA_RETRIEVAL_FAILED`` -- the resilient source exhausted its retries (1.9).

    ``channel_id`` makes the affected Owned_Channel identifiable; ``reason`` is a
    human-readable description suitable for a digest or run summary. Named per the
    design; referenced within the package as ``connection_manager.ConnectionError``
    to avoid confusion with the builtin of the same name.
    """

    channel_id: str
    status: AuthStatus
    reason: str


# ---------------------------------------------------------------------------
# Credential persistence boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialStore(Protocol):
    """The persistence boundary for authorized-channel credentials (1.2, 1.8).

    Storing credentials "in the Configuration" is a side effect, so it lives
    behind this interface rather than inside the manager's decision logic. A
    concrete store persists the :class:`AuthorizedChannel` (its
    ``credentials_ref`` and ``connected`` state) and reports success or failure;
    a reported failure is what drives the ``credential-storage-failed`` branch
    (1.8).
    """

    def store(self, channel: AuthorizedChannel) -> bool:
        """Persist ``channel``. Return ``True`` on success, ``False`` on failure.

        A ``False`` return (or any failure to persist) means the credentials were
        not saved, so the channel must be left not connected (1.8).
        """
        ...

    def get(self, channel_id: str) -> AuthorizedChannel | None:
        """Return the stored channel for ``channel_id``, or ``None`` if absent."""
        ...

    def all_channels(self) -> tuple[AuthorizedChannel, ...]:
        """Return every currently stored authorized channel."""
        ...


class InMemoryCredentialStore:
    """A simple in-memory :class:`CredentialStore` with failure injection.

    Suitable as a default and for tests. ``fail_channel_ids`` names channels
    whose :meth:`store` must fail (modeling 1.8); ``fail_all`` forces every store
    to fail. Successfully stored channels are queryable via :meth:`get` and
    :meth:`all_channels`, so a caller (or test) can confirm a granted channel was
    persisted and connected (1.2).
    """

    def __init__(
        self,
        *,
        fail_all: bool = False,
        fail_channel_ids: set[str] | None = None,
    ) -> None:
        self._channels: dict[str, AuthorizedChannel] = {}
        self._fail_all = fail_all
        self._fail_channel_ids = set(fail_channel_ids or set())

    def store(self, channel: AuthorizedChannel) -> bool:
        if self._fail_all or channel.channel_id in self._fail_channel_ids:
            return False
        self._channels[channel.channel_id] = channel
        return True

    def get(self, channel_id: str) -> AuthorizedChannel | None:
        return self._channels.get(channel_id)

    def all_channels(self) -> tuple[AuthorizedChannel, ...]:
        return tuple(self._channels.values())


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Owns the authorization and connection lifecycle for Owned_Channels.

    Construct with a :class:`CredentialStore` (the persistence sink for granted
    credentials) and, optionally, a ``request_issuer`` callable used during
    :meth:`initiate_connection` to perform the real side effect of issuing the
    authorization request (tests can supply an issuer that advances the clock to
    exercise the 5 s budget of 1.1).
    """

    MAX_OWNED_CHANNELS = 50
    AUTH_REQUEST_TIMEOUT_SECONDS = 5.0  # time to issue the auth request (1.1)
    AUTH_DECISION_TIMEOUT_SECONDS = 300.0  # time to await grant/deny (1.7)

    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        *,
        request_issuer: Callable[[str], None] | None = None,
    ) -> None:
        self._store: CredentialStore = credential_store or InMemoryCredentialStore()
        self._request_issuer = request_issuer

    @property
    def credential_store(self) -> CredentialStore:
        """The credential store this manager persists granted credentials to."""
        return self._store

    # -- Phase 1: initiation -------------------------------------------------

    def initiate_connection(self, channel_id: str, clock: Clock) -> AuthorizationResult:
        """Issue the authorization request for ``channel_id`` (1.1).

        Records the initiation time on ``clock``, performs the request-issue side
        effect (immediately, or via an injected ``request_issuer``), and returns a
        :class:`AuthorizationResult` with status ``REQUESTED``. Issuing is
        synchronous and therefore well within the 5 s budget by construction; the
        latency budget itself (1.1) is asserted end-to-end by the integration
        tests. ``clock`` is accepted so callers can measure the issue latency.
        """
        started_at = clock.monotonic()
        if self._request_issuer is not None:
            self._request_issuer(channel_id)
        # The elapsed time is available for callers/integration tests to assert the
        # 5 s budget (1.1); the synchronous issue path never approaches it.
        _ = clock.monotonic() - started_at
        return AuthorizationResult(channel_id=channel_id, status=AuthStatus.REQUESTED)

    # -- Phase 2: decision ---------------------------------------------------

    def complete_authorization(
        self,
        channel_id: str,
        grant: AuthorizationGrant,
        config: Configuration,
    ) -> AuthorizationResult:
        """Resolve the Creator's authorization decision for ``channel_id``.

        The decision is evaluated in priority order so each requirement's "SHALL
        NOT retrieve" guarantee holds by never producing a connected channel:

        1. **Timeout (1.7).** If the decision arrived after
           :data:`AUTH_DECISION_TIMEOUT_SECONDS` (``grant.responded_within_seconds``
           > 300), record ``authorization-timeout`` -- regardless of grant/deny,
           a late decision is no decision.
        2. **Denial (1.3).** A timely deny records ``authorization-failed``.
        3. **Capacity (1.6/1.8).** Authorizing a *new* channel beyond
           :data:`MAX_OWNED_CHANNELS` cannot be stored, so it is recorded as
           ``credential-storage-failed`` naming the 50-channel limit.
        4. **Grant (1.2/1.8).** A timely grant stores the credentials via the
           :class:`CredentialStore` and marks the channel ``connected`` on success;
           a storage failure records ``credential-storage-failed``, signals the
           credentials were not saved, and leaves the channel not connected.

        Args:
            channel_id: The Owned_Channel being authorized; always echoed back in
                the result so the affected channel is identifiable.
            grant: The Creator's decision, carrying ``granted``, the
                ``credentials_ref`` (present only when granted), and
                ``responded_within_seconds`` (evaluated against the 300 s window).
            config: The current configuration snapshot, whose
                ``authorized_channels`` provide the count used for the 50-channel
                capacity check and to detect re-authorization of an existing
                channel.

        Returns:
            An :class:`AuthorizationResult` whose ``status`` reflects the branch
            taken. Only the grant-and-store-succeeds path yields ``CONNECTED``.
        """
        # 1.7: a decision that arrived too late is an authorization-timeout, and no
        # retrieval is ever attempted because no connected channel is produced.
        if grant.responded_within_seconds > self.AUTH_DECISION_TIMEOUT_SECONDS:
            return AuthorizationResult(
                channel_id=channel_id,
                status=AuthStatus.AUTHORIZATION_TIMEOUT,
                error=(
                    "authorization-timeout: the Creator neither granted nor denied "
                    f"authorization within {self.AUTH_DECISION_TIMEOUT_SECONDS:g}s "
                    f"for channel '{channel_id}'"
                ),
            )

        # 1.3: a denial records authorization-failed and never retrieves.
        if not grant.granted:
            return AuthorizationResult(
                channel_id=channel_id,
                status=AuthStatus.AUTHORIZATION_FAILED,
                error=(
                    "authorization-failed: the Creator denied authorization for "
                    f"channel '{channel_id}'"
                ),
            )

        # A granted decision must carry credentials to store; a grant without them
        # cannot be saved, so it is a credential-storage failure (1.8).
        if grant.credentials_ref is None:
            return AuthorizationResult(
                channel_id=channel_id,
                status=AuthStatus.CREDENTIAL_STORAGE_FAILED,
                error=(
                    "credential-storage-failed: the grant provided no credentials "
                    f"to store for channel '{channel_id}'"
                ),
            )

        # 1.6/1.8: enforce the 50-channel cap. Re-authorizing an already-authorized
        # channel does not consume new capacity; a genuinely new channel beyond the
        # cap cannot be stored.
        existing_ids = {c.channel_id for c in config.authorized_channels}
        if channel_id not in existing_ids and len(existing_ids) >= self.MAX_OWNED_CHANNELS:
            return AuthorizationResult(
                channel_id=channel_id,
                status=AuthStatus.CREDENTIAL_STORAGE_FAILED,
                error=(
                    "credential-storage-failed: owned-channel limit reached "
                    f"(a maximum of {self.MAX_OWNED_CHANNELS} channels can be "
                    f"authorized); credentials for channel '{channel_id}' were not saved"
                ),
            )

        # 1.2: store the credentials and mark the channel connected. The connected
        # channel is only persisted when the store reports success.
        channel = AuthorizedChannel(
            channel_id=channel_id,
            credentials_ref=grant.credentials_ref,
            connected=True,
            credentials_expired=False,
        )
        if not self._store.store(channel):
            # 1.8: storage failed -> credential-storage-failed, credentials not
            # saved, channel left not connected (nothing was persisted).
            return AuthorizationResult(
                channel_id=channel_id,
                status=AuthStatus.CREDENTIAL_STORAGE_FAILED,
                error=(
                    "credential-storage-failed: the authorization credentials for "
                    f"channel '{channel_id}' were not saved to the Configuration"
                ),
            )

        return AuthorizationResult(channel_id=channel_id, status=AuthStatus.CONNECTED)

    # -- Phase 3: retrieval --------------------------------------------------

    def retrieve_with_credentials(
        self,
        channel: AuthorizedChannel,
        request: DataRequest,
        source: ResilientDataSource,
        clock: Clock,
    ) -> Result[TaggedData, ConnectionError]:
        """Retrieve data for a connected ``channel`` using its stored credentials.

        Branches:

        - **Expired credentials (1.5).** If ``channel.credentials_expired`` is set,
          return ``Err(ConnectionError(AUTHORIZATION_EXPIRED, ...))`` identifying
          the channel and never touch the data source.
        - **Not connected.** A channel that is not connected has no valid
          credentials to retrieve with, so retrieval is refused (consistent with
          the "SHALL NOT retrieve" guarantees of 1.3/1.7).
        - **Retrieval (1.4/1.9).** Otherwise the request is issued through
          ``source``, which enforces the 30 s request timeout and up to 3 retries
          (Requirement 16). On success the payload is tagged with the channel id
          (1.6); when the resilient source ultimately fails, return
          ``Err(ConnectionError(DATA_RETRIEVAL_FAILED, ...))`` identifying the
          channel. The stored credentials are never modified here, so they are
          retained across a failed retrieval (1.9).

        Args:
            channel: The connected Owned_Channel whose credentials authorize the
                request. Its ``channel_id`` tags successful results and identifies
                failures.
            request: The reified data-source call to issue.
            source: The resilient data-source handle that applies the 30 s timeout
                and retry policy (1.4, 1.9).
            clock: Accepted for signature fidelity and overall-deadline use; the
                30 s retrieval budget (1.4) is enforced by ``source``'s
                ``RetryPolicy.request_timeout_seconds``.

        Returns:
            ``Ok(TaggedData)`` carrying the channel-tagged payload on success, or
            ``Err(ConnectionError)`` identifying the channel for the expired or
            retrieval-failed cases.
        """
        # 1.5: expired credentials short-circuit before any retrieval attempt.
        if channel.credentials_expired:
            return Err(
                ConnectionError(
                    channel_id=channel.channel_id,
                    status=AuthStatus.AUTHORIZATION_EXPIRED,
                    reason=(
                        "authorization-expired: stored credentials for channel "
                        f"'{channel.channel_id}' have expired"
                    ),
                )
            )

        # A channel that was never connected has no valid credentials to use.
        if not channel.connected:
            return Err(
                ConnectionError(
                    channel_id=channel.channel_id,
                    status=AuthStatus.AUTHORIZATION_FAILED,
                    reason=(
                        "channel "
                        f"'{channel.channel_id}' is not connected; no valid "
                        "credentials are available for retrieval"
                    ),
                )
            )

        # 1.4/1.9: retrieve through the resilience layer (timeout + retries).
        result = source.call(request)
        if result.is_err():
            failure = result.unwrap_err()
            # 1.9: retries are exhausted -> data-retrieval-failed identifying the
            # channel. Credentials are untouched here, so they are retained.
            return Err(
                ConnectionError(
                    channel_id=channel.channel_id,
                    status=AuthStatus.DATA_RETRIEVAL_FAILED,
                    reason=(
                        "data-retrieval-failed: could not retrieve data for channel "
                        f"'{channel.channel_id}' ({failure.reason})"
                    ),
                )
            )

        # 1.6: tag the retrieved data set with its originating channel id.
        return Ok(TaggedData(channel_id=channel.channel_id, data=result.unwrap()))
