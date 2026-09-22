"""Logging behaviour that the design depends on: correlation and redaction.

Redaction is asserted rather than assumed because a log aggregator is usually far
more widely readable than MongoDB, and Snapshots hold real credentials.
"""

import logging

import pytest

from app.config import Environment, Settings
from app.logging import ApplyIdFilter, RedactionFilter, apply_id_var, redact


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (Environment.NP, "DEBUG"),
        (Environment.TEST, "DEBUG"),
        (Environment.PREP, "INFO"),
        (Environment.PROD, "INFO"),
    ],
)
def test_log_level_defaults_per_environment(environment: Environment, expected: str) -> None:
    assert Settings(environment=environment).effective_log_level == expected


def test_explicit_log_level_overrides_the_environment_default() -> None:
    """The level is wanted most during a production incident, so it must not be a
    pure function of environment type."""
    settings = Settings(environment=Environment.PROD, log_level="debug")

    assert settings.effective_log_level == "DEBUG"


def test_secret_bearing_fields_are_redacted() -> None:
    payload = {
        "catalog": "finance",
        "connection-password": "hunter2",
        "nested": {"private_key": "-----BEGIN-----", "host": "db.internal"},
    }

    result = redact(payload)

    assert result["connection-password"] == "[redacted]"
    assert result["nested"]["private_key"] == "[redacted]"
    assert result["catalog"] == "finance"
    assert result["nested"]["host"] == "db.internal"


def test_secrets_inline_in_a_message_are_redacted() -> None:
    assert "hunter2" not in redact("connection-password=hunter2 host=db")


def test_redaction_applies_to_records_at_every_level() -> None:
    """Never conditional: a filter switched on per level or per environment is one
    configuration mistake away from not redacting."""
    redaction = RedactionFilter()

    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
        record = logging.LogRecord(
            "t", level, "", 0, "creating catalog with password=hunter2", None, None
        )
        record.connection_password = "hunter2"

        redaction.filter(record)

        assert "hunter2" not in record.getMessage()
        assert record.connection_password == "[redacted]"


def test_apply_id_is_attached_to_records() -> None:
    """One identifier yields the whole story of a failure; without it the lines of
    several concurrent pollers interleave and no log level makes them readable."""
    record = logging.LogRecord("t", logging.INFO, "", 0, "applying", None, None)

    token = apply_id_var.set("apl_123")
    try:
        ApplyIdFilter().filter(record)
    finally:
        apply_id_var.reset(token)

    assert record.apply_id == "apl_123"


def test_records_outside_an_apply_carry_no_identifier() -> None:
    record = logging.LogRecord("t", logging.INFO, "", 0, "starting", None, None)

    ApplyIdFilter().filter(record)

    assert record.apply_id is None


def test_credentials_embedded_in_a_url_are_redacted() -> None:
    """mongodb.connection-url carries user:pass in the value, and its name matches
    no secret pattern -- so the value has to be inspected, not just the key."""
    result = redact({"mongodb.connection-url": "mongodb://trino:hunter2@db:27017/x"})

    assert "hunter2" not in result["mongodb.connection-url"]
    assert result["mongodb.connection-url"].startswith("mongodb://trino:")


def test_a_url_without_credentials_is_untouched() -> None:
    assert redact("https://example.com/path") == "https://example.com/path"
