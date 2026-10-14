"""Logging behaviour that the design depends on: correlation and redaction.

Redaction is asserted rather than assumed because a log aggregator is usually far
more widely readable than MongoDB, and Snapshots hold real credentials.
"""

import logging

import pytest

from app.config import Environment, Settings
from app.logging import (
    ApplyIdFilter,
    RedactionFilter,
    apply_id_var,
    configure_logging,
    redact,
)


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


#: Attributes logging sets on every LogRecord itself. Passing one of these in `extra` raises
#: KeyError("Attempt to overwrite ...") *at the call site*, so the failure only appears when
#: that line runs -- which for a log line in an uncommon path can be a long time after it
#: shipped. A token mint did exactly this (#131).
RESERVED_ON_A_LOG_RECORD = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def test_no_log_call_passes_a_reserved_key_in_extra() -> None:
    """Statically, over the whole application, because the alternative is finding out on the
    day the unlucky code path runs.

    Read with `ast` rather than by importing: the point is to cover every log call in `app/`,
    including the ones no test exercises.
    """
    import ast
    from pathlib import Path

    offenders: list[str] = []
    for path in sorted(Path(__file__).resolve().parents[2].joinpath("app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in RESERVED_ON_A_LOG_RECORD:
                        offenders.append(f"{path.name}:{key.lineno} extra={{{key.value!r}: ...}}")

    assert offenders == [], f"these log calls raise KeyError when they run: {offenders}"


def test_a_dependencys_debug_output_stays_off() -> None:
    """`configure_logging` raises the level on Apchi's namespace, never on the root.

    This is not tidiness, it is what keeps §12's credentials out of the log. PyMongo's
    `pymongo.command` logger prints **every command Apchi sends, including the document**, so
    at DEBUG it would dump Snapshots -- which hold real connection passwords and private keys
    -- straight into the log, past a redaction filter that keys on field names and would never
    see them.

    The same argument covers the Kubernetes client and httpx. `app/logging.py` states the
    reasoning; nothing asserted it until a raw-files test turned the root logger up and found
    MongoDB printing the file it had just written.
    """
    configure_logging(Settings(log_level="DEBUG"))

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("app").getEffectiveLevel() == logging.DEBUG

    # Not DEBUG, rather than exactly WARNING. The trino client sets its own logger to INFO on
    # import -- `trino/logging.py` calls `setLevel` on the `trino` root -- so a dependency can
    # overrule the level chosen here and asserting equality would fail on a library's own
    # choice rather than on anything Apchi did. What matters for secrets is the DEBUG
    # threshold: INFO lines are messages, DEBUG lines are payloads.
    at_debug = [
        name
        for name in ("pymongo", "pymongo.command", "httpx", "kubernetes", "trino")
        if logging.getLogger(name).getEffectiveLevel() <= logging.DEBUG
    ]

    assert at_debug == [], (
        f"these would log at DEBUG: {at_debug}. pymongo.command at DEBUG prints every "
        "document Apchi writes -- Snapshots included, which hold real connection passwords "
        "and private keys (§12) -- past a filter that keys on field names and would never "
        "see them."
    )
