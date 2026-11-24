"""MongoDB access. PyMongo's async API, not Motor -- Motor reached end of life in
May 2026 and is in critical-fixes-only support."""

import logging
from typing import Any

from pymongo import AsyncMongoClient, monitoring
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings
from app.trust import bundled_ca, combined, recorded

logger = logging.getLogger(__name__)


class _CommandLog(monitoring.CommandListener):
    """Each command at DEBUG: what it was, which collection, and how long it took.

    PyMongo's own `pymongo.command` logger is the alternative and it is off on purpose: it
    prints the command document, which is a Snapshot with real credentials in it. A listener
    sees the same events and can choose to print only their names.
    """

    def __init__(self) -> None:
        # The collection is named only in the started event, which carries the document;
        # the outcome arrives separately. Keyed as PyMongo keys a command in flight.
        self._targets: dict[tuple[Any, int], str] = {}

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        collection = event.command.get(event.command_name)
        target = f"{event.database_name}.{collection}" if isinstance(collection, str) else ""
        self._targets[(event.connection_id, event.request_id)] = target or event.database_name

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        self._log(event, "ok")

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        self._log(event, str(event.failure.get("codeName", "failed")))

    def _log(
        self, event: monitoring.CommandSucceededEvent | monitoring.CommandFailedEvent, outcome: str
    ) -> None:
        target = self._targets.pop((event.connection_id, event.request_id), event.database_name)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "mongo: %s %s -> %s in %dms",
                event.command_name,
                target,
                outcome,
                event.duration_micros // 1000,
            )


def _tls_options(settings: Settings) -> dict[str, Any]:
    """The TLS files, as PyMongo keyword arguments.

    Keyword arguments rather than query parameters appended to `mongo_uri`, so that a typo
    is a `ConfigurationError` at startup instead of an option PyMongo quietly does not
    recognise. Either one also turns TLS on by itself -- PyMongo builds its SSL context from
    these and does not need `tls=true` as well (#186).
    """
    # Any, not str: PyMongo declares its first few options by name and the rest as **kwargs,
    # so a dict[str, str] expanded into it is checked against `tz_aware: bool | None`.
    options: dict[str, Any] = {}
    # The concatenated bundle, for the reason app.trust gives: PyMongo replaces its trust
    # store with whatever path it is handed.
    bundle = bundled_ca(settings.ca_file)
    if bundle:
        options["tlsCAFile"] = bundle
    if settings.mongo_certificate_key_file:
        # The mounted path, not a copy: PyMongo loads it into a context while validating its
        # options, so it is already read exactly once (#193). `recorded` only remembers what
        # it held, so a later rotation can be reported.
        options["tlsCertificateKeyFile"] = recorded(settings.mongo_certificate_key_file)
    elif joined := combined(settings.mongo_certificate_file, settings.mongo_key_file):
        options["tlsCertificateKeyFile"] = joined
    return options


class Mongo:
    def __init__(self, settings: Settings) -> None:
        try:
            self._client: AsyncMongoClient = AsyncMongoClient(
                settings.mongo_uri, event_listeners=[_CommandLog()], **_tls_options(settings)
            )
        except FileNotFoundError as missing:
            # PyMongo opens these while validating its options, so this raises here rather
            # than on the first query -- and `Mongo` is built in the lifespan, so Apchi
            # crashes instead of answering 503 like an unreachable MongoDB does. Re-raised
            # naming the setting, because the original says only which path was missing and
            # the path was assembled by the chart from a mount point and a Secret key.
            raise RuntimeError(
                f"Apchi cannot start: {missing.filename} does not exist. It comes from "
                "mongo_certificate_key_file, or from mongo_certificate_file and "
                "mongo_key_file -- under Helm, a key mongodb.tls names that its Secret does "
                "not hold. A missing CA is reported by app.trust instead, since that one is "
                "shared with every other destination."
            ) from missing
        self._database = self._client[settings.mongo_database]

    @property
    def database(self) -> AsyncDatabase:
        return self._database

    async def ping(self) -> bool:
        """True when the server answers. Used by the health endpoint."""
        try:
            await self._client.admin.command("ping")
        except Exception:
            return False
        return True

    async def close(self) -> None:
        await self._client.close()
