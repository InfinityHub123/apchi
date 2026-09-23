"""MongoDB access. PyMongo's async API, not Motor -- Motor reached end of life in
May 2026 and is in critical-fixes-only support."""

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings


class Mongo:
    def __init__(self, settings: Settings) -> None:
        self._client: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri)
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
