"""Trino access.

The official client is synchronous, so queries go through a threadpool. Only
/v1/statement is documented public API; liveness endpoints go through httpx.
Cluster membership comes from the system.runtime.nodes system table rather than
/v1/node, which returns 404 on Trino 483.
"""

from typing import Any

import httpx
from fastapi.concurrency import run_in_threadpool


class Trino:
    def __init__(self, host: str, port: int = 8080, user: str = "apchi") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._base = f"http://{host}:{port}"

    def _query_sync(self, sql: str) -> list[tuple[Any, ...]]:
        import trino

        conn = trino.dbapi.connect(host=self._host, port=self._port, user=self._user)
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            return list(cursor.fetchall())
        finally:
            conn.close()

    async def query(self, sql: str) -> list[tuple[Any, ...]]:
        return await run_in_threadpool(self._query_sync, sql)

    async def is_starting(self) -> bool | None:
        """None when the coordinator cannot be reached at all."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base}/v1/info")
                response.raise_for_status()
                return bool(response.json().get("starting", True))
        except Exception:
            return None

    async def active_worker_count(self) -> int:
        rows = await self.query(
            "SELECT count(*) FROM system.runtime.nodes WHERE NOT coordinator AND state = 'active'"
        )
        return int(rows[0][0]) if rows else 0

    async def catalogs(self) -> set[str]:
        return {row[0] for row in await self.query("SHOW CATALOGS")}
