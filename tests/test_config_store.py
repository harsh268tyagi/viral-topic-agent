"""Unit tests for configuration serialization and ConfigurationStore (task 3.1).

These cover the concrete Requirement 15 branches:

- successful write sets ``saved`` (15.2),
- load deserializes the persisted config (15.3),
- corrupt load -> ``configuration-invalid`` naming the failing setting, without
  overwriting the persisted data (15.5),
- write failure -> ``configuration-save`` naming the failing setting, previous
  config retained (15.6),
- empty store -> ``configuration-missing`` (15.7).

The lossless round-trip property (15.4) is exercised here with a few concrete
examples; the universal property-based test for it lives in task 3.2.
"""

from __future__ import annotations

import pytest

from viral_topic_agent.domain import models as m
from viral_topic_agent.persistence.config_store import (
    CONFIG_INVALID,
    CONFIG_MISSING,
    SAVE_FAILED,
    SAVED,
    ConfigDeserializationError,
    ConfigSerializationError,
    ConfigurationStore,
    InMemoryStorageBackend,
    StorageWriteError,
    deserialize_config,
    serialize_config,
)
from viral_topic_agent.infrastructure.result import Err, Ok


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _empty_config() -> m.Configuration:
    return m.Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(),
    )


def _full_config() -> m.Configuration:
    return m.Configuration(
        authorized_channels=(
            m.AuthorizedChannel(
                channel_id="ch1",
                credentials_ref="ref-1",
                connected=True,
                credentials_expired=False,
            ),
            m.AuthorizedChannel(
                channel_id="ch2",
                credentials_ref="ref-2",
                connected=False,
                credentials_expired=True,
            ),
        ),
        selected_category=m.ChannelCategory.GAMING,
        monitored_competitors=("comp1", "comp2", "comp3"),
        schedule=m.Schedule(recurrence_interval="daily", run_time="08:00"),
        delivery_destinations=(
            m.DeliveryDestination.EMAIL,
            m.DeliveryDestination.SLACK,
            m.DeliveryDestination.NOTION,
        ),
    )


# ---------------------------------------------------------------------------
# serialize/deserialize round-trip examples (15.1, 15.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", [_empty_config(), _full_config()])
def test_round_trip_is_field_by_field_equal(config: m.Configuration):
    """serialize then deserialize reproduces an equal Configuration (15.1, 15.4)."""
    restored = deserialize_config(serialize_config(config))
    assert restored == config


def test_serialize_produces_json_string():
    """The serialized form is a JSON string covering the five persisted settings."""
    import json

    blob = serialize_config(_full_config())
    assert isinstance(blob, str)
    payload = json.loads(blob)
    assert set(payload) == {
        "authorized_channels",
        "selected_category",
        "monitored_competitors",
        "schedule",
        "delivery_destinations",
    }


def test_schedule_with_partial_fields_round_trips():
    """A schedule with null fields still round-trips losslessly."""
    config = m.Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=m.Schedule(recurrence_interval=None, run_time="09:30"),
        delivery_destinations=(),
    )
    assert deserialize_config(serialize_config(config)) == config


# ---------------------------------------------------------------------------
# 15.2 / 15.3 - successful save then load
# ---------------------------------------------------------------------------


def test_save_sets_saved_status_on_success():
    """A successful write reports the saved status (15.2)."""
    store = ConfigurationStore(InMemoryStorageBackend())
    result = store.save(_full_config())
    assert result.status == SAVED
    assert result.saved is True
    assert result.failing_setting is None


def test_load_deserializes_persisted_config():
    """Loading returns the previously saved configuration (15.3)."""
    backend = InMemoryStorageBackend()
    store = ConfigurationStore(backend)
    config = _full_config()

    save_result = store.save(config)
    assert save_result.status == SAVED

    load_result = store.load()
    assert isinstance(load_result, Ok)
    assert load_result.unwrap() == config


# ---------------------------------------------------------------------------
# 15.5 - corrupt load -> configuration-invalid, no overwrite
# ---------------------------------------------------------------------------


def test_corrupt_data_returns_configuration_invalid_without_overwrite():
    """Corrupt persisted data -> configuration-invalid; data is not overwritten (15.5)."""
    corrupt = "{ this is not valid json"
    backend = InMemoryStorageBackend(initial=corrupt)
    store = ConfigurationStore(backend)

    result = store.load()
    assert isinstance(result, Err)
    err = result.unwrap_err()
    assert err.kind == CONFIG_INVALID
    assert err.failing_setting is not None
    # The store must not have overwritten the persisted (corrupt) data on load.
    assert backend.read() == corrupt


def test_invalid_setting_is_named_in_configuration_invalid():
    """A malformed setting is named in the configuration-invalid error (15.5)."""
    # Valid JSON, but selected_category holds an unsupported value.
    blob = (
        '{"authorized_channels": [], "selected_category": "cooking", '
        '"monitored_competitors": [], "schedule": null, '
        '"delivery_destinations": []}'
    )
    store = ConfigurationStore(InMemoryStorageBackend(initial=blob))

    result = store.load()
    assert isinstance(result, Err)
    err = result.unwrap_err()
    assert err.kind == CONFIG_INVALID
    assert err.failing_setting == "selected_category"


def test_missing_setting_is_named_in_configuration_invalid():
    """A missing required setting is named in the configuration-invalid error (15.5)."""
    blob = '{"authorized_channels": []}'  # other settings absent
    store = ConfigurationStore(InMemoryStorageBackend(initial=blob))

    result = store.load()
    assert isinstance(result, Err)
    err = result.unwrap_err()
    assert err.kind == CONFIG_INVALID
    assert err.failing_setting == "selected_category"


def test_deserialize_raises_named_error_for_corrupt_blob():
    """deserialize_config raises a named ConfigDeserializationError on bad data."""
    with pytest.raises(ConfigDeserializationError) as exc_info:
        deserialize_config("not json at all")
    assert exc_info.value.failing_setting is not None


# ---------------------------------------------------------------------------
# 15.6 - write failure -> configuration-save, previous config retained
# ---------------------------------------------------------------------------


def test_write_failure_returns_configuration_save_and_retains_previous():
    """A write failure yields configuration-save and retains the previous config (15.6)."""
    previous = _full_config()
    previous_blob = serialize_config(previous)

    def failing_writer(_blob: str) -> None:
        raise StorageWriteError("disk full", failing_setting="delivery_destinations")

    backend = InMemoryStorageBackend(initial=previous_blob, writer=failing_writer)
    store = ConfigurationStore(backend)

    # Attempt to save a different configuration; the write fails.
    new_config = _empty_config()
    result = store.save(new_config)

    assert result.status == SAVE_FAILED
    assert result.failing_setting == "delivery_destinations"

    # The previously persisted configuration must be retained unchanged.
    assert backend.read() == previous_blob
    reloaded = store.load()
    assert isinstance(reloaded, Ok)
    assert reloaded.unwrap() == previous


def test_write_failure_without_named_setting_falls_back_to_document():
    """A generic backend failure still reports configuration-save (15.6)."""

    def boom(_blob: str) -> None:
        raise StorageWriteError("backend unavailable")

    store = ConfigurationStore(InMemoryStorageBackend(writer=boom))
    result = store.save(_full_config())
    assert result.status == SAVE_FAILED
    assert result.failing_setting is not None


def test_serialization_failure_names_setting():
    """A setting that cannot be serialized surfaces as configuration-save (15.6)."""

    # Build a Configuration whose selected_category is a bogus object lacking
    # ``.value`` to trigger an encoder failure for that named setting.
    class Bogus:
        pass

    bad = m.Configuration(
        authorized_channels=(),
        selected_category=Bogus(),  # type: ignore[arg-type]
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(),
    )
    store = ConfigurationStore(InMemoryStorageBackend())
    result = store.save(bad)
    assert result.status == SAVE_FAILED
    assert result.failing_setting == "selected_category"


def test_serialize_config_raises_named_error_for_bad_setting():
    """serialize_config raises a named ConfigSerializationError on a bad setting."""

    class Bogus:
        pass

    bad = m.Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(Bogus(),),  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigSerializationError) as exc_info:
        serialize_config(bad)
    assert exc_info.value.failing_setting == "delivery_destinations"


# ---------------------------------------------------------------------------
# 15.7 - empty store -> configuration-missing
# ---------------------------------------------------------------------------


def test_empty_store_returns_configuration_missing():
    """Loading from an empty store yields configuration-missing (15.7)."""
    store = ConfigurationStore(InMemoryStorageBackend())
    result = store.load()
    assert isinstance(result, Err)
    assert result.unwrap_err().kind == CONFIG_MISSING


def test_configuration_missing_carries_no_failing_setting():
    """The missing notification is distinct from invalid (no failing setting)."""
    store = ConfigurationStore(InMemoryStorageBackend())
    err = store.load().unwrap_err()
    assert err.kind == CONFIG_MISSING
    assert err.failing_setting is None
