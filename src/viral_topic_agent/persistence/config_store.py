"""Configuration persistence with round-trip integrity (Requirement 15).

This module provides:

- ``serialize_config`` / ``deserialize_config`` — explicit JSON encoders and
  decoders for every field of :class:`~viral_topic_agent.models.Configuration`.
  Serialization is deliberately explicit (one encoder per setting) rather than
  relying on a generic framework, so the lossless field-by-field round-trip
  required by 15.4 is fully under our control and so a failure can *name the
  failing setting* (15.5, 15.6).
- :class:`ConfigurationStore` — saves and loads a ``Configuration`` against a
  pluggable :class:`StorageBackend`. The backend abstraction lets tests inject
  write failures and corrupt data deterministically.

Behavioral contract (Requirement 15):

- ``save`` serializes the five persisted settings (authorized channels, selected
  category, monitored competitors, schedule, delivery destinations) (15.1) and,
  on a successful write, reports status ``saved`` (15.2). If serialization or the
  write fails, it returns a ``configuration-save`` error naming the failing
  setting and the previously persisted configuration is retained unchanged
  (15.6).
- ``load`` deserializes the persisted configuration (15.3). Corrupt or
  unreadable data yields a ``configuration-invalid`` error naming the failing
  setting, without overwriting what is persisted (15.5). When nothing is
  persisted it yields a ``configuration-missing`` notification (15.7).

The settings handled here map one-to-one to ``Configuration`` fields and to the
named-setting requirement language, so error messages can point the Creator at
exactly what went wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Protocol

from viral_topic_agent.domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    Configuration,
    DeliveryDestination,
    Schedule,
)
from viral_topic_agent.infrastructure.result import Err, Ok, Result

__all__ = [
    "serialize_config",
    "deserialize_config",
    "ConfigurationStore",
    "StorageBackend",
    "InMemoryStorageBackend",
    "FileStorageBackend",
    "SaveResult",
    "ConfigError",
    "ConfigSerializationError",
    "ConfigDeserializationError",
    "StorageWriteError",
    "SAVED",
    "SAVE_FAILED",
    "CONFIG_INVALID",
    "CONFIG_MISSING",
]


# ---------------------------------------------------------------------------
# Status / error string constants (match the requirement language)
# ---------------------------------------------------------------------------

SAVED = "saved"  # 15.2
SAVE_FAILED = "configuration-save"  # 15.6
CONFIG_INVALID = "configuration-invalid"  # 15.5
CONFIG_MISSING = "configuration-missing"  # 15.7

# Sentinel used when a failure cannot be attributed to a single named setting
# (e.g. the whole document is not valid JSON, or a raw storage write fails).
DOCUMENT = "<document>"

# The five persisted settings named by Requirement 15.1, in serialization order.
SETTING_AUTHORIZED_CHANNELS = "authorized_channels"
SETTING_SELECTED_CATEGORY = "selected_category"
SETTING_MONITORED_COMPETITORS = "monitored_competitors"
SETTING_SCHEDULE = "schedule"
SETTING_DELIVERY_DESTINATIONS = "delivery_destinations"


# ---------------------------------------------------------------------------
# Exceptions raised by the (de)serialization codecs
# ---------------------------------------------------------------------------


class ConfigSerializationError(Exception):
    """Raised when a ``Configuration`` cannot be serialized.

    Carries ``failing_setting`` so the caller can surface a
    ``configuration-save`` error that names the offending setting (15.6).
    """

    def __init__(self, failing_setting: str, reason: str) -> None:
        super().__init__(f"failed to serialize setting '{failing_setting}': {reason}")
        self.failing_setting = failing_setting
        self.reason = reason


class ConfigDeserializationError(Exception):
    """Raised when persisted data cannot be deserialized.

    Carries ``failing_setting`` so the caller can surface a
    ``configuration-invalid`` error that names the offending setting (15.5).
    """

    def __init__(self, failing_setting: str, reason: str) -> None:
        super().__init__(
            f"failed to deserialize setting '{failing_setting}': {reason}"
        )
        self.failing_setting = failing_setting
        self.reason = reason


class StorageWriteError(Exception):
    """Raised by a storage backend when a write fails.

    A backend may attach a ``failing_setting`` (e.g. when the failure is tied to
    a particular setting); otherwise the store reports the document sentinel.
    """

    def __init__(self, reason: str, failing_setting: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failing_setting = failing_setting


# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    """Outcome of :meth:`ConfigurationStore.save`.

    On success ``status == SAVED`` (15.2). On failure ``status == SAVE_FAILED``
    (``"configuration-save"``) and ``failing_setting`` names the setting that
    could not be persisted (15.6).
    """

    status: str
    failing_setting: str | None = None
    error: str | None = None

    @property
    def saved(self) -> bool:
        return self.status == SAVED


@dataclass(frozen=True)
class ConfigError:
    """Error returned by :meth:`ConfigurationStore.load`.

    ``kind`` is either ``configuration-invalid`` (15.5) or
    ``configuration-missing`` (15.7). For an invalid configuration,
    ``failing_setting`` names the setting that could not be read.
    """

    kind: str
    failing_setting: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Per-setting JSON encoders
# ---------------------------------------------------------------------------


def _encode_authorized_channel(channel: AuthorizedChannel) -> dict:
    return {
        "channel_id": channel.channel_id,
        "credentials_ref": channel.credentials_ref,
        "connected": bool(channel.connected),
        "credentials_expired": bool(channel.credentials_expired),
    }


def _encode_schedule(schedule: Schedule | None) -> dict | None:
    if schedule is None:
        return None
    return {
        "recurrence_interval": schedule.recurrence_interval,
        "run_time": schedule.run_time,
    }


def _encode_setting(name: str, encode: Callable[[], object]) -> object:
    """Run a per-setting encoder, tagging any failure with the setting name."""
    try:
        return encode()
    except ConfigSerializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a named codec error
        raise ConfigSerializationError(name, str(exc)) from exc


def serialize_config(config: Configuration) -> str:
    """Serialize a :class:`Configuration` to a JSON string (15.1).

    Each of the five persisted settings is encoded explicitly. If a setting
    cannot be encoded, a :class:`ConfigSerializationError` naming that setting is
    raised so the caller can report a ``configuration-save`` error (15.6).
    """
    if not isinstance(config, Configuration):
        raise ConfigSerializationError(
            DOCUMENT, f"expected Configuration, got {type(config).__name__}"
        )

    payload = {
        SETTING_AUTHORIZED_CHANNELS: _encode_setting(
            SETTING_AUTHORIZED_CHANNELS,
            lambda: [_encode_authorized_channel(c) for c in config.authorized_channels],
        ),
        SETTING_SELECTED_CATEGORY: _encode_setting(
            SETTING_SELECTED_CATEGORY,
            lambda: None
            if config.selected_category is None
            else config.selected_category.value,
        ),
        SETTING_MONITORED_COMPETITORS: _encode_setting(
            SETTING_MONITORED_COMPETITORS,
            lambda: [str(c) for c in config.monitored_competitors],
        ),
        SETTING_SCHEDULE: _encode_setting(
            SETTING_SCHEDULE, lambda: _encode_schedule(config.schedule)
        ),
        SETTING_DELIVERY_DESTINATIONS: _encode_setting(
            SETTING_DELIVERY_DESTINATIONS,
            lambda: [d.value for d in config.delivery_destinations],
        ),
    }

    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ConfigSerializationError(DOCUMENT, f"JSON encoding failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Per-setting JSON decoders
# ---------------------------------------------------------------------------


def _decode_authorized_channels(value: object) -> tuple[AuthorizedChannel, ...]:
    if not isinstance(value, list):
        raise ValueError("expected a list of authorized channels")
    channels: list[AuthorizedChannel] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each authorized channel must be an object")
        channels.append(
            AuthorizedChannel(
                channel_id=_require_str(item, "channel_id"),
                credentials_ref=_require_str(item, "credentials_ref"),
                connected=_require_bool(item, "connected"),
                credentials_expired=_require_bool(item, "credentials_expired"),
            )
        )
    return tuple(channels)


def _decode_selected_category(value: object) -> ChannelCategory | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("selected category must be a string or null")
    return ChannelCategory(value)  # raises ValueError on an unsupported value


def _decode_monitored_competitors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("expected a list of competitor ids")
    competitors: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each competitor id must be a string")
        competitors.append(item)
    return tuple(competitors)


def _decode_schedule(value: object) -> Schedule | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("schedule must be an object or null")
    if "recurrence_interval" not in value or "run_time" not in value:
        raise ValueError("schedule must contain 'recurrence_interval' and 'run_time'")
    interval = value["recurrence_interval"]
    run_time = value["run_time"]
    if interval is not None and not isinstance(interval, str):
        raise ValueError("recurrence_interval must be a string or null")
    if run_time is not None and not isinstance(run_time, str):
        raise ValueError("run_time must be a string or null")
    return Schedule(recurrence_interval=interval, run_time=run_time)


def _decode_delivery_destinations(value: object) -> tuple[DeliveryDestination, ...]:
    if not isinstance(value, list):
        raise ValueError("expected a list of delivery destinations")
    destinations: list[DeliveryDestination] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each delivery destination must be a string")
        destinations.append(DeliveryDestination(item))  # raises ValueError if invalid
    return tuple(destinations)


def _require_str(obj: dict, key: str) -> str:
    if key not in obj:
        raise ValueError(f"missing required field '{key}'")
    val = obj[key]
    if not isinstance(val, str):
        raise ValueError(f"field '{key}' must be a string")
    return val


def _require_bool(obj: dict, key: str) -> bool:
    if key not in obj:
        raise ValueError(f"missing required field '{key}'")
    val = obj[key]
    if not isinstance(val, bool):
        raise ValueError(f"field '{key}' must be a boolean")
    return val


def _decode_setting(name: str, raw: dict, decode: Callable[[object], object]) -> object:
    """Run a per-setting decoder, tagging any failure with the setting name."""
    if name not in raw:
        raise ConfigDeserializationError(name, "missing setting")
    try:
        return decode(raw[name])
    except ConfigDeserializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a named codec error
        raise ConfigDeserializationError(name, str(exc)) from exc


def deserialize_config(blob: str) -> Configuration:
    """Deserialize a JSON string into a :class:`Configuration` (15.3).

    Raises :class:`ConfigDeserializationError` naming the failing setting when
    the data is corrupt or a setting is malformed (15.5).
    """
    try:
        raw = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ConfigDeserializationError(DOCUMENT, f"not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigDeserializationError(
            DOCUMENT, "top-level configuration must be a JSON object"
        )

    authorized_channels = _decode_setting(
        SETTING_AUTHORIZED_CHANNELS, raw, _decode_authorized_channels
    )
    selected_category = _decode_setting(
        SETTING_SELECTED_CATEGORY, raw, _decode_selected_category
    )
    monitored_competitors = _decode_setting(
        SETTING_MONITORED_COMPETITORS, raw, _decode_monitored_competitors
    )
    schedule = _decode_setting(SETTING_SCHEDULE, raw, _decode_schedule)
    delivery_destinations = _decode_setting(
        SETTING_DELIVERY_DESTINATIONS, raw, _decode_delivery_destinations
    )

    return Configuration(
        authorized_channels=authorized_channels,  # type: ignore[arg-type]
        selected_category=selected_category,  # type: ignore[arg-type]
        monitored_competitors=monitored_competitors,  # type: ignore[arg-type]
        schedule=schedule,  # type: ignore[arg-type]
        delivery_destinations=delivery_destinations,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Storage backend abstraction
# ---------------------------------------------------------------------------


class StorageBackend(Protocol):
    """Pluggable persistent-storage backend for serialized configuration.

    ``read`` returns the persisted blob, or ``None`` when nothing is persisted
    (drives the ``configuration-missing`` path, 15.7). ``write`` persists the
    blob and may raise :class:`StorageWriteError` to signal a write failure
    (drives the ``configuration-save`` path, 15.6).
    """

    def read(self) -> str | None: ...

    def write(self, blob: str) -> None: ...


class InMemoryStorageBackend:
    """In-memory backend, primarily for tests.

    An optional ``writer`` hook is invoked with the blob *before* it is stored;
    raising from the hook simulates a write failure while leaving the previously
    stored blob untouched (so the store can satisfy "retain the previous config",
    15.6).
    """

    def __init__(
        self,
        initial: str | None = None,
        *,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._data = initial
        self._writer = writer

    def read(self) -> str | None:
        return self._data

    def write(self, blob: str) -> None:
        if self._writer is not None:
            # The hook may raise (e.g. StorageWriteError) to simulate failure.
            # It runs before the assignment, so a failure retains the old value.
            self._writer(blob)
        self._data = blob


class FileStorageBackend:
    """Backend that persists the serialized configuration to a file.

    ``read`` returns ``None`` when the file does not exist (nothing persisted).
    Writes go through a temporary file and an atomic replace so a failed write
    leaves any previously persisted file unchanged.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def read(self) -> str | None:
        import os

        if not os.path.exists(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise StorageWriteError(f"could not read configuration file: {exc}") from exc

    def write(self, blob: str) -> None:
        import os
        import tempfile

        directory = os.path.dirname(os.path.abspath(self._path))
        try:
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(blob)
                os.replace(tmp_path, self._path)
            except OSError:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except OSError as exc:
            raise StorageWriteError(f"could not write configuration file: {exc}") from exc


# ---------------------------------------------------------------------------
# ConfigurationStore
# ---------------------------------------------------------------------------


class ConfigurationStore:
    """Persists and reloads a :class:`Configuration` (Requirement 15).

    The store delegates the actual bytes to a :class:`StorageBackend`, so the
    storage medium (in-memory, file, future database) is interchangeable and
    failures are injectable for testing.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    def save(self, config: Configuration) -> SaveResult:
        """Serialize and persist ``config``.

        Returns a :class:`SaveResult` with status ``saved`` on success (15.2).
        On a serialization or write failure, returns a ``configuration-save``
        error naming the failing setting; the previously persisted configuration
        is left unchanged (15.6).
        """
        try:
            blob = serialize_config(config)
        except ConfigSerializationError as exc:
            return SaveResult(
                status=SAVE_FAILED,
                failing_setting=exc.failing_setting,
                error=exc.reason,
            )

        try:
            self._backend.write(blob)
        except StorageWriteError as exc:
            return SaveResult(
                status=SAVE_FAILED,
                failing_setting=exc.failing_setting or DOCUMENT,
                error=exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 - any backend failure is a save failure
            return SaveResult(
                status=SAVE_FAILED,
                failing_setting=DOCUMENT,
                error=str(exc),
            )

        return SaveResult(status=SAVED)

    def load(self) -> Result[Configuration, ConfigError]:
        """Load and deserialize the persisted configuration.

        Returns ``Ok(Configuration)`` on success (15.3). Returns
        ``Err(ConfigError(configuration-missing))`` when nothing is persisted
        (15.7), or ``Err(ConfigError(configuration-invalid))`` naming the failing
        setting when the persisted data is corrupt — without overwriting what is
        persisted (15.5).
        """
        try:
            blob = self._backend.read()
        except StorageWriteError as exc:
            # An unreadable store is treated as invalid rather than missing; we
            # never overwrite on a read failure.
            return Err(
                ConfigError(kind=CONFIG_INVALID, failing_setting=DOCUMENT, reason=exc.reason)
            )

        if blob is None:
            return Err(ConfigError(kind=CONFIG_MISSING))

        try:
            config = deserialize_config(blob)
        except ConfigDeserializationError as exc:
            return Err(
                ConfigError(
                    kind=CONFIG_INVALID,
                    failing_setting=exc.failing_setting,
                    reason=exc.reason,
                )
            )

        return Ok(config)
