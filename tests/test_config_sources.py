"""Unit tests for the layered configuration sources (task 2.2).

Covers the :class:`ConfigurationSource` protocol and the four concrete sources
in ``config/sources.py`` -- ``OverridesSource``, ``EnvSource``,
``DotEnvSource``, ``ConfigFileSource`` -- plus the pure ``.env`` parser
(``parse_dotenv``).

These example-based tests pin the source contracts and the ``.env`` parsing
rules of Requirements 10.4 and 10.6: comment and blank lines are ignored, every
other line is split at the first ``=`` with key/value trimmed, and a non-blank,
non-comment line with no ``=`` is reported as malformed by line number while
contributing no value. (The exhaustive property test for ``.env`` parsing is
task 2.3.)
"""

from config.sources import (
    ConfigFileSource,
    ConfigurationSource,
    DotEnvSource,
    EnvSource,
    OverridesSource,
    parse_dotenv,
)


# ---------------------------------------------------------------------------
# parse_dotenv -- the .env parsing rules (10.4, 10.6)
# ---------------------------------------------------------------------------


def test_parse_dotenv_ignores_blank_and_comment_lines():
    text = "\n   \n# a comment\n   # indented comment\nKEY=value\n"
    result = parse_dotenv(text)
    assert dict(result.values) == {"KEY": "value"}
    assert result.malformed_lines == ()


def test_parse_dotenv_trims_key_and_value_whitespace():
    result = parse_dotenv("  SPACED_KEY   =   spaced value  ")
    assert dict(result.values) == {"SPACED_KEY": "spaced value"}


def test_parse_dotenv_splits_only_at_the_first_equals():
    result = parse_dotenv("CONNECTION=key=with=equals")
    assert dict(result.values) == {"CONNECTION": "key=with=equals"}


def test_parse_dotenv_allows_empty_value():
    result = parse_dotenv("EMPTY=")
    assert dict(result.values) == {"EMPTY": ""}
    assert result.malformed_lines == ()


def test_parse_dotenv_reports_malformed_line_by_number_and_skips_value():
    text = "GOOD=1\nNO_EQUALS_HERE\nALSO_GOOD=2"
    result = parse_dotenv(text)
    assert dict(result.values) == {"GOOD": "1", "ALSO_GOOD": "2"}
    assert result.malformed_lines == (2,)


def test_parse_dotenv_reports_every_malformed_line():
    text = "BAD_ONE\nKEY=value\nBAD_TWO\n# comment\nBAD_THREE"
    result = parse_dotenv(text)
    assert dict(result.values) == {"KEY": "value"}
    assert result.malformed_lines == (1, 3, 5)


def test_parse_dotenv_last_assignment_wins_for_duplicate_keys():
    result = parse_dotenv("DUP=first\nDUP=second")
    assert dict(result.values) == {"DUP": "second"}


def test_parse_dotenv_handles_crlf_line_endings():
    result = parse_dotenv("A=1\r\nBAD\r\nB=2")
    assert dict(result.values) == {"A": "1", "B": "2"}
    assert result.malformed_lines == (2,)


# ---------------------------------------------------------------------------
# OverridesSource (highest precedence)
# ---------------------------------------------------------------------------


def test_overrides_source_exposes_values_as_strings():
    source = OverridesSource({"PORT": 587, "ENABLED": True, "NAME": "x"})
    assert source.values() == {"PORT": "587", "ENABLED": "True", "NAME": "x"}


def test_overrides_source_returns_a_defensive_copy():
    source = OverridesSource({"A": "1"})
    returned = source.values()
    returned["A"] = "tampered"
    assert source.values() == {"A": "1"}


def test_overrides_source_empty_when_no_overrides():
    assert OverridesSource().values() == {}


# ---------------------------------------------------------------------------
# EnvSource (process environment variables)
# ---------------------------------------------------------------------------


def test_env_source_snapshots_supplied_environment():
    environ = {"YT_API_KEY": "abc", "OTHER": "x"}
    source = EnvSource(environ)
    environ["YT_API_KEY"] = "changed"
    assert source.values()["YT_API_KEY"] == "abc"


def test_env_source_reads_only_requested_keys_when_given():
    source = EnvSource({"A": "1", "B": "2", "C": "3"}, keys=["A", "C", "MISSING"])
    assert source.values() == {"A": "1", "C": "3"}


# ---------------------------------------------------------------------------
# DotEnvSource (a local .env file)
# ---------------------------------------------------------------------------


def test_dotenv_source_parses_text_and_exposes_malformed_lines():
    source = DotEnvSource(text="A=1\nMALFORMED\nB=2")
    assert source.values() == {"A": "1", "B": "2"}
    assert source.malformed_lines == (2,)


def test_dotenv_source_reads_from_a_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# header\nKEY=value\nNO_EQUALS", encoding="utf-8")
    source = DotEnvSource(env_file)
    assert source.values() == {"KEY": "value"}
    assert source.malformed_lines == (3,)


def test_dotenv_source_missing_file_contributes_nothing(tmp_path):
    source = DotEnvSource(tmp_path / "does-not-exist.env")
    assert source.values() == {}
    assert source.malformed_lines == ()


# ---------------------------------------------------------------------------
# ConfigFileSource (configuration-file defaults, lowest precedence)
# ---------------------------------------------------------------------------


def test_config_file_source_coerces_scalars_and_skips_complex_values():
    source = ConfigFileSource(
        values={
            "name": "agent",
            "port": 587,
            "ratio": 1.5,
            "enabled": True,
            "disabled": False,
            "absent": None,
            "nested": {"k": "v"},
            "listy": [1, 2],
        }
    )
    assert source.values() == {
        "name": "agent",
        "port": "587",
        "ratio": "1.5",
        "enabled": "true",
        "disabled": "false",
    }


def test_config_file_source_reads_json_file(tmp_path):
    config_file = tmp_path / "defaults.json"
    config_file.write_text('{"request_timeout_seconds": 30, "model": "gpt"}', encoding="utf-8")
    source = ConfigFileSource(config_file)
    assert source.values() == {"request_timeout_seconds": "30", "model": "gpt"}


def test_config_file_source_missing_file_is_empty(tmp_path):
    assert ConfigFileSource(tmp_path / "missing.json").values() == {}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_all_sources_satisfy_the_configuration_source_protocol():
    assert isinstance(OverridesSource(), ConfigurationSource)
    assert isinstance(EnvSource({}), ConfigurationSource)
    assert isinstance(DotEnvSource(text=""), ConfigurationSource)
    assert isinstance(ConfigFileSource(values={}), ConfigurationSource)
