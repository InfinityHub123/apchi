"""MongoDB access. PyMongo's async API, not Motor -- Motor reached end of life in
May 2026 and is in critical-fixes-only support."""

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings
from app.trust import bundled_ca


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
        options["tlsCertificateKeyFile"] = settings.mongo_certificate_key_file
    return options


class Mongo:
    def __init__(self, settings: Settings) -> None:
        try:
            self._client: AsyncMongoClient = AsyncMongoClient(
                settings.mongo_uri, **_tls_options(settings)
            )
        except FileNotFoundError as missing:
            # PyMongo opens these while validating its options, so this raises here rather
            # than on the first query -- and `Mongo` is built in the lifespan, so Apchi
            # crashes instead of answering 503 like an unreachable MongoDB does. Re-raised
            # naming the setting, because the original says only which path was missing and
            # the path was assembled by the chart from a mount point and a Secret key.
            raise RuntimeError(
                f"Apchi cannot start: {missing.filename} does not exist. It comes from "
                "mongo_certificate_key_file -- under Helm, mongodb.tls.certificateKeyKey "
                "naming a key that Secret does not hold. A missing CA is reported by "
                "app.trust instead, since that one is shared with every other destination."
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
