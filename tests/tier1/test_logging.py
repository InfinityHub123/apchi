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


# --- Timestamps and debugging mode ------------------------------------------------------------

import json  # noqa: E402
import re  # noqa: E402
from typing import Any  # noqa: E402

from testcontainers.community.mongodb import MongoDbContainer  # noqa: E402

from app.adapters.kubernetes import RealKubernetes  # noqa: E402
from app.adapters.mongo import Mongo  # noqa: E402
from app.adapters.trino import Query, Trino  # noqa: E402
from app.logging import ConsoleFormatter, JsonFormatter  # noqa: E402

#: 2026-11-22T14:03:07.512Z
ISO_UTC = re.compile(r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def _record(message: str = "applying", exc_info: Any = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "app.pipeline.applies", logging.INFO, "", 42, message, None, exc_info
    )
    record.apply_id = None
    return record


def test_console_lines_carry_an_iso_timestamp_in_utc() -> None:
    """Not `formatTime`'s local time with no zone: Apchi's lines are read beside Trino's and
    the API server's, and only UTC lets a reader line the three up."""
    assert ISO_UTC.match(ConsoleFormatter().format(_record()))


def test_json_lines_carry_an_iso_timestamp_in_utc() -> None:
    assert ISO_UTC.match(json.loads(JsonFormatter().format(_record()))["time"])


def test_console_lines_keep_the_traceback() -> None:
    """`format` is overridden whole, so nothing appended the exception: every
    `logger.exception` in np and test -- the chart's default -- printed no stack trace."""
    try:
        raise ValueError("the actual cause")
    except ValueError:
        import sys

        line = ConsoleFormatter().format(_record("it failed", sys.exc_info()))

    assert "Traceback" in line
    assert "ValueError: the actual cause" in line


def test_debugging_mode_names_where_each_line_came_from() -> None:
    assert "app.pipeline.applies:42 " in ConsoleFormatter(debug=True).format(_record())
    assert "app.pipeline.applies:42" not in ConsoleFormatter().format(_record())
    assert json.loads(JsonFormatter(debug=True).format(_record()))["line"] == 42
    assert "line" not in json.loads(JsonFormatter().format(_record()))


def test_debugging_mode_lowers_the_level_even_in_production() -> None:
    assert Settings(environment=Environment.PROD, debug=True).effective_log_level == "DEBUG"


def test_an_explicit_level_still_wins_over_debugging_mode() -> None:
    settings = Settings(environment=Environment.PROD, debug=True, log_level="info")

    assert settings.effective_log_level == "INFO"


def test_debugging_mode_leaves_dependencies_debug_output_off() -> None:
    """The obvious debugging mode -- everything at DEBUG -- is the one that leaks Snapshots
    and Secrets. See `test_a_dependencys_debug_output_stays_off`."""
    configure_logging(Settings(debug=True))

    at_debug = [
        name
        for name in ("pymongo", "pymongo.command", "httpx", "httpcore", "kubernetes", "urllib3")
        if logging.getLogger(name).getEffectiveLevel() <= logging.DEBUG
    ]

    assert at_debug == []
    assert logging.getLogger("app").getEffectiveLevel() == logging.DEBUG


def test_debugging_mode_announces_itself(capsys: pytest.CaptureFixture[str]) -> None:
    """At WARNING, because it is most often switched on in an incident and forgotten."""
    configure_logging(Settings(debug=True, log_json=False))

    assert re.search(r"WARNING +app\.logging:\d+ debugging mode is on", capsys.readouterr().err)


def test_uvicorns_lines_are_timestamped_and_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    """Uvicorn's own handlers printed its startup and request lines with no time and past the
    redaction filter. Its loggers are configured before the app is imported, so that is
    reproduced here before `configure_logging` runs."""
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.StreamHandler()]
    access.propagate = False

    configure_logging(Settings(log_json=False))
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET", "/x?token=hunter2", "1.1", 200)

    err = capsys.readouterr().err
    line = next(line for line in err.splitlines() if "GET /x" in line)
    assert ISO_UTC.match(line)
    assert "hunter2" not in err


async def test_kubernetes_calls_are_logged_by_name_never_by_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Secret's body is base64 with no field name beside it, so no redaction catches it --
    which is why the client's own debug output stays off and this line exists."""

    def create_namespaced_secret(namespace: str, body: dict[str, Any]) -> None:
        return None

    kubernetes = RealKubernetes(namespace="trino")
    body = {"metadata": {"name": "trino-catalog-seed"}, "stringData": {"pg": "aHVudGVyMg=="}}

    with caplog.at_level(logging.DEBUG, logger="app.adapters.kubernetes"):
        await kubernetes._call(create_namespaced_secret, "trino", body)

    assert re.search(
        r"kubernetes: create_namespaced_secret trino-catalog-seed -> ok in \d+ms", caplog.text
    )
    assert "aHVudGVyMg" not in caplog.text


async def test_a_refused_kubernetes_call_logs_its_status(caplog: pytest.LogCaptureFixture) -> None:
    from kubernetes.client.exceptions import ApiException

    def read_namespaced_secret(name: str, namespace: str) -> None:
        raise ApiException(status=404)

    with (
        caplog.at_level(logging.DEBUG, logger="app.adapters.kubernetes"),
        pytest.raises(ApiException),
    ):
        await RealKubernetes(namespace="trino")._call(read_namespaced_secret, "absent", "trino")

    assert "kubernetes: read_namespaced_secret absent -> 404" in caplog.text


async def test_create_catalog_is_logged_without_its_values(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redaction filter finds a secret by the name beside it, and in
    `'connection-password' = 'hunter2'` a quote stands between them. So the statement is
    logged with every value withheld, rather than trusted to the filter."""
    trino = Trino("http://trino:8080")
    monkeypatch.setattr(trino, "_execute", lambda sql, patience: Query(rows=[], query_id="q_1"))

    with caplog.at_level(logging.DEBUG, logger="app.adapters.trino"):
        await trino.create_catalog(
            "finance", "postgresql", {"connection-password": "hunter2", "connection-user": "u"}
        )

    assert (
        'trino: CREATE CATALOG "finance" USING postgresql WITH '
        '("connection-password" = ..., "connection-user" = ...) -> 0 rows' in caplog.text
    )
    assert "hunter2" not in caplog.text
    assert "(q_1)" in caplog.text


async def test_mongo_commands_are_logged_by_collection_never_by_document(
    caplog: pytest.LogCaptureFixture, mongo_container: MongoDbContainer
) -> None:
    """`pymongo.command` would print the document -- a Snapshot, with real credentials."""
    mongo = Mongo(Settings(mongo_uri=mongo_container.get_connection_url()))
    try:
        with caplog.at_level(logging.DEBUG, logger="app.adapters.mongo"):
            await mongo.database["debug_log"].insert_one({"password": "hunter2"})
    finally:
        await mongo.close()

    assert re.search(r"mongo: insert \w+\.debug_log -> ok in \d+ms", caplog.text)
    assert "hunter2" not in caplog.text
