"""Trino access.

The official client is synchronous, so queries go through a threadpool. Only
/v1/statement is documented public API; liveness endpoints go through httpx.
Cluster membership comes from the system.runtime.nodes system table rather than
/v1/node, which returns 404 on Trino 483.
"""

import logging
import re
import time
from typing import Any, NamedTuple

import httpx
from fastapi.concurrency import run_in_threadpool

#: What Apchi's own queries report as their source. The client would otherwise send
#: "trino-python-client", which says nothing about who is asking. Named so that an Operator
#: can see Apchi's queries in `system.runtime.queries` -- and so a Resource Group selector
#: can route them deliberately, which is what makes Verification able to predict where its
#: own query lands.
SOURCE = "apchi"

logger = logging.getLogger(__name__)

#: The name used for both the catalog and the connector in `connector_factories`. The same
#: word twice so that a reader of Trino's query history -- where this lands as a failed
#: query -- can tell what it was for without asking anyone.
_PROBE = "apchi_connector_probe"

_FACTORIES = re.compile(r"Available factories: \[([^\]]*)\]")


class _Patience(NamedTuple):
    """How long a query may take, for the one query a caller waits on.

    The client defaults to a 30s timeout and three attempts, so a coordinator that accepts
    connections and then goes quiet can hold a request for a minute and a half. Every other
    query here runs inside an Apply, where that is the right trade; `connector_factories`
    runs while an Operator is waiting for a `POST /catalogs` to answer, where it is not.
    """

    request_timeout: float
    max_attempts: int


_PROBE_PATIENCE = _Patience(request_timeout=5.0, max_attempts=1)

#: How long a connector list is believed, and how long a failure to read one is left alone.
#:
#: Both directions matter. A list that never expired would miss a plugin an Admin added by
#: changing Trino's image, which does not restart Apchi; a failure that was never retried
#: would leave the check off until someone noticed. And caching the failure at all is what
#: keeps a coordinator that is down from costing every single catalog request its timeout.
_CONNECTORS_TTL_SECONDS = 600.0


class Query(NamedTuple):
    """A query's result and the identifier Trino filed it under.

    The identifier is what turns a smoke query into evidence: Trino records which resource
    group a query ran in, and that row is found by id.
    """

    rows: list[tuple[Any, ...]]
    query_id: str


class Trino:
    def __init__(self, host: str, port: int = 8080, user: str = "apchi") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._base = f"http://{host}:{port}"
        self._connectors: frozenset[str] | None = None
        self._connectors_read_at: float | None = None

    def _query_sync(self, sql: str, patience: _Patience | None = None) -> Query:
        import trino

        conn = trino.dbapi.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            source=SOURCE,
            **(patience._asdict() if patience else {}),
        )
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            return Query(rows=list(cursor.fetchall()), query_id=str(cursor.query_id))
        finally:
            conn.close()

    async def query(self, sql: str) -> list[tuple[Any, ...]]:
        return (await run_in_threadpool(self._query_sync, sql)).rows

    async def run(self, sql: str) -> Query:
        """The rows and the query id. Used where what happened to the query matters as much
        as what it returned."""
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

    async def queries_at_risk(self) -> int:
        """Queries a coordinator restart would destroy: running and queued alike.

        The counting query is itself running while it counts, so it is excluded --
        verified against a coordinator with nothing else in flight, which still reported
        one.
        """
        rows = await self.query(
            "SELECT count(*) FROM system.runtime.queries WHERE state IN ('RUNNING', 'QUEUED')"
        )
        counted = int(rows[0][0]) if rows else 0
        return max(counted - 1, 0)

    async def catalogs(self) -> set[str]:
        return {row[0] for row in await self.query("SHOW CATALOGS")}

    async def catalog_connectors(self) -> dict[str, str]:
        """Every loaded catalog and the connector behind it.

        As much as Trino will say about a catalog, and the reason Adoption cannot be a
        matter of asking it: there is no properties column here, no `SHOW CREATE CATALOG`
        in 483 -- the grammar accepts only FUNCTION, MATERIALIZED, SCHEMA, TABLE and VIEW --
        and no other route to a `connection-url`. Verified against a running coordinator.

        So this is what tells Apchi a catalog *exists* that it cannot reconstruct, which is
        worth more than it sounds: a catalog created by DDL after the pod started lives only
        in the coordinator's writable store, and without this it would simply be absent from
        a discovery and deleted at the first restart after adoption (§15).
        """
        rows = await self.query("SELECT catalog_name, connector_name FROM system.metadata.catalogs")
        return {row[0]: row[1] for row in rows}

    async def create_catalog(self, name: str, connector: str, properties: dict[str, str]) -> None:
        """Issues CREATE CATALOG. Trino writes the .properties file itself as a side
        effect, so Apchi never touches the coordinator's store directory."""
        rendered = ", ".join(
            f"{_identifier(key)} = {_literal(value)}" for key, value in sorted(properties.items())
        )
        clause = f" WITH ({rendered})" if rendered else ""
        await self.query(
            f"CREATE CATALOG {_identifier(name)} USING {_connector_name(connector)}{clause}"
        )

    async def connector_factories(self) -> frozenset[str] | None:
        """The connectors this coordinator can load, cached, or None if it would not say.

        Read lazily rather than at startup, because Apchi starts whether or not Trino is up
        and a list read once at boot would be empty on every Cluster that came up second.
        Cached either way, including the failure -- see `_CONNECTORS_TTL_SECONDS`.
        """
        fresh = (
            self._connectors_read_at is not None
            and time.monotonic() - self._connectors_read_at < _CONNECTORS_TTL_SECONDS
        )
        if not fresh:
            self._connectors = await self._read_connector_factories()
            self._connectors_read_at = time.monotonic()
        return self._connectors

    async def _read_connector_factories(self) -> frozenset[str] | None:
        """Every connector this coordinator can actually load, or None if it would not say.

        Trino enumerates its own factories when asked for one it does not have:

            No factory for connector 'apchi_connector_probe'. Available factories:
            [ai, bigquery, blackhole, cassandra, ..., trino_thrift]

        Which is the only authority worth using. The names are not the ones the
        documentation prints -- `delta_lake`, `gsheets` and `trino_thrift`, not "Delta Lake",
        "Google Sheets" and "Thrift" -- nor the ones in /usr/lib/trino/plugin, which spells
        one of them `delta-lake` and also holds event listeners and authenticators that are
        not connectors at all. Asking the coordinator also covers a custom plugin baked into
        the image, which no list Apchi shipped ever could.

        Two things about the statement are load-bearing, both found by trying it:

        * The catalog name must not already exist. Trino checks existence *before* it looks
          for a factory, so `CREATE CATALOG memory USING <unknown>` answers "Catalog 'memory'
          already exists" and names no factories.
        * No `IF NOT EXISTS`. With it the statement succeeds and does nothing, which is
          exactly the error this depends on being raised.

        Returns None rather than raising, and None means "no opinion": see
        `app/sections/catalogs/connectors.py` for why every unreadable answer has to end in
        nothing being refused.
        """
        import trino.exceptions

        try:
            await run_in_threadpool(
                self._query_sync, f"CREATE CATALOG {_PROBE} USING {_PROBE}", _PROBE_PATIENCE
            )
        except trino.exceptions.TrinoQueryError as exc:
            found = _FACTORIES.search(str(exc.message))
            if found is None:
                # A different refusal than the one being provoked -- a leftover catalog of
                # this name, or a message Trino has since reworded.
                logger.warning(
                    "could not read the connector list from Trino's refusal",
                    extra={"trino_message": str(exc.message)[:200]},
                )
                return None
            names = frozenset(name.strip() for name in found.group(1).split(",") if name.strip())
            logger.info("read the connector list from Trino", extra={"connectors": len(names)})
            return names
        except Exception:
            logger.warning("could not ask Trino for its connector list", exc_info=True)
            return None

        # The probe connector exists after all, so the statement created a catalog. Nothing
        # else can be concluded, and the catalog has to go.
        logger.warning(
            "the connector probe created a catalog; dropping it", extra={"catalog": _PROBE}
        )
        try:
            await self.drop_catalog(_PROBE)
        except Exception:
            logger.exception("could not drop the connector probe catalog")
        return None

    async def drop_catalog(self, name: str) -> None:
        """Issues DROP CATALOG. This permanently deletes the backing .properties
        file, and the Hive, Iceberg, Delta Lake and Hudi connectors are documented
        as not releasing all resources when a catalog is dropped."""
        await self.query(f"DROP CATALOG {_identifier(name)}")


_CONNECTOR_NAME = re.compile(r"\A[a-z0-9_-]+\Z")


def _connector_name(value: str) -> str:
    """Bare, never delimited. Trino rejects a quoted identifier after USING: the
    quotes land inside the name it validates, so "memory" is not the memory
    connector."""
    if not _CONNECTOR_NAME.match(value):
        raise ValueError(f"unusable connector name: {value!r}")
    return value


def _identifier(value: str) -> str:
    """A quoted SQL identifier. Catalog names are constrained by their model, but
    pass-through property keys are arbitrary strings and often contain hyphens."""
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _literal(value: str) -> str:
    """A SQL string literal. Property values are Operator-supplied, so they are
    escaped rather than interpolated."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"
