"""Logging: correlation by Apply id, and redaction that is never conditional.

The redaction filter runs at every level in every environment. A filter that is
switched on per environment is one configuration mistake away from not redacting,
and non-production credentials are credentials to real systems.
"""

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

# Set for the duration of an Apply so one identifier yields the whole story of a
# failure. Several things poll concurrently during an Apply; without this their
# lines interleave and no log level makes them readable.
apply_id_var: ContextVar[str | None] = ContextVar("apply_id", default=None)

REDACTED = "[redacted]"

# Field names known to carry secrets. Matched case-insensitively against both
# structured extras and key=value pairs in message text.
_SECRET_NAMES = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "privatekey",
    "ssl_key",
    "sslkey",
    "connection-password",
    "connection_password",
    "authorization",
    "api_key",
    "apikey",
    "keytab",
)

# Credentials embedded in a URL, e.g. mongodb://user:pass@host:27017. The property
# name (mongodb.connection-url) matches no secret pattern, so the value has to be
# inspected rather than the key.
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")

# An Apchi token, by value. The names below catch a secret sitting next to a field name that
# looks secret-bearing; a token logged on its own -- in a message, in a URL, in an exception
# from something that was handed one -- has no such neighbour. The `apchi_` prefix exists so
# this rule can exist, which is the same trick as the URL credentials above.
_TOKEN_VALUE = re.compile(r"\bapchi_[A-Za-z0-9_-]{16,}")
_INLINE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(n) for n in _SECRET_NAMES) + r")\b(\s*[=:]\s*)(\S+)"
)


def _is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(s in lowered for s in _SECRET_NAMES)


def redact(value: Any) -> Any:
    """Recursively replace secret-bearing values. Used by the filter and by callers
    building structured log payloads."""
    if isinstance(value, dict):
        return {k: (REDACTED if _is_secret_name(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = _INLINE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
        value = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", value)
        return _TOKEN_VALUE.sub(REDACTED, value)
    return value


class ApplyIdFilter(logging.Filter):
    """Injects the in-flight Apply's identifier into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.apply_id = apply_id_var.get()
        return True


class RedactionFilter(logging.Filter):
    """Redacts secret-bearing fields from the message and from structured extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(a) for a in record.args)
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED:
                continue
            record.__dict__[key] = REDACTED if _is_secret_name(key) else redact(value)
        return True


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "apply_id",
    "message",
    "asctime",
    "taskName",
    # Uvicorn attaches an ANSI-coloured copy of every startup line. It is the same message
    # again, with escape codes a log aggregator would store verbatim.
    "color_message",
}


def timestamp(record: logging.LogRecord) -> str:
    """ISO 8601 in UTC, to the millisecond: `2026-11-22T14:03:07.512Z`.

    Not `Formatter.formatTime`, which writes local time with no zone and a comma before the
    milliseconds. Apchi's lines are read next to Trino's and the API server's, from pods
    whose clocks agree only in UTC, so a line has to say which instant it means.
    """
    moment = datetime.fromtimestamp(record.created, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    def __init__(self, debug: bool = False) -> None:
        super().__init__()
        self._debug = debug

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self._debug:
            payload["line"] = record.lineno
        apply_id = getattr(record, "apply_id", None)
        if apply_id:
            payload["apply_id"] = apply_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "apply_id":
                payload[key] = value
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self, debug: bool = False) -> None:
        super().__init__()
        self._debug = debug

    def format(self, record: logging.LogRecord) -> str:
        apply_id = getattr(record, "apply_id", None)
        prefix = f"[{apply_id}] " if apply_id else ""
        origin = f"{record.name}:{record.lineno} " if self._debug else ""
        line = f"{timestamp(record)} {record.levelname:<7} {origin}{prefix}{record.getMessage()}"
        # `format` is overridden whole, so the base class never appends the traceback; every
        # `logger.exception` printed its message and lost its stack trace.
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


#: Uvicorn's loggers. It gives them handlers of its own that do not propagate, so its startup
#: lines and its one line per request printed with no timestamp and past the redaction filter.
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(settings: Settings) -> None:
    formatter = JsonFormatter if settings.effective_log_json else ConsoleFormatter
    handler = logging.StreamHandler()
    handler.setFormatter(formatter(debug=settings.debug))
    handler.addFilter(ApplyIdFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)

    # The handler is attached at the root so every record is formatted, filtered
    # and correlated the same way -- including records from libraries. But the
    # *level* is set on Apchi's own namespace, not the root: an application that
    # puts the root at DEBUG turns on every dependency's debug output, which
    # drowns the signal the level was raised to find.
    #
    # Debugging mode does not change that. The obvious debugging mode -- every library at
    # DEBUG -- is the one that leaks: `pymongo.command` prints each document Apchi sends, and
    # the Kubernetes client prints every response body, Secrets included. Neither can be
    # redacted, because a BSON document and a base64 Secret have no field names for the
    # filter to recognise. So Apchi logs its own outbound calls instead, by name and timing,
    # in the adapters.
    root.setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(settings.effective_log_level)

    # Uvicorn configures logging before it imports this application, so its handlers already
    # exist here and are replaced rather than prevented.
    for name in _UVICORN:
        server = logging.getLogger(name)
        server.handlers.clear()
        server.propagate = True
    logging.getLogger("uvicorn").setLevel(logging.INFO)

    if settings.environment is not None:
        logging.getLogger(__name__).warning(
            "APCHI_ENVIRONMENT is deprecated and removed in the next release. It now decides "
            "only the log defaults; set APCHI_LOG_LEVEL and APCHI_LOG_JSON instead",
            extra={"environment": settings.environment},
        )

    if settings.debug:
        # A warning so that it stands out in a production log, where it is most likely to
        # have been switched on during an incident and then forgotten.
        logging.getLogger(__name__).warning(
            "debugging mode is on", extra={"log_level": settings.effective_log_level}
        )
