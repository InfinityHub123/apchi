"""Logging: correlation by Apply id, and redaction that is never conditional.

The redaction filter runs at every level in every environment. A filter that is
switched on per environment is one configuration mistake away from not redacting,
and non-production credentials are credentials to real systems.
"""

import json
import logging
import re
from contextvars import ContextVar
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
        return _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", value)
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
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
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
    def format(self, record: logging.LogRecord) -> str:
        apply_id = getattr(record, "apply_id", None)
        prefix = f"[{apply_id}] " if apply_id else ""
        return f"{self.formatTime(record)} {record.levelname:<7} {prefix}{record.getMessage()}"


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.effective_log_json else ConsoleFormatter())
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
    root.setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(settings.effective_log_level)
