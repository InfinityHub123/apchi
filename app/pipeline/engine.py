"""The real Apply engine.

Walks the Candidate through Apply, Verification and Commit against the actual
Cluster. Validation is still a stub: it arrives with the ephemeral Trino.

The ordering that matters most is in `apply`: the Secret is patched *before* the DDL
is issued.
"""

import logging
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.candidate import CandidateStore
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.verification import verify
from app.sections import SectionName
from app.sections.catalogs import apply as catalog_apply
from app.sections.catalogs.generator import render_secret
from app.sections.catalogs.section import SECTION

logger = logging.getLogger(__name__)


class Engine:
    """One instance per Apply: it carries the state the stages share."""

    def __init__(
        self,
        apply_id: str,
        settings: Settings,
        candidates: CandidateStore,
        snapshots: SnapshotStore,
        trino: Trino,
        kubernetes: KubernetesAdapter,
    ) -> None:
        self._apply_id = apply_id
        self._settings = settings
        self._candidates = candidates
        self._snapshots = snapshots
        self._trino = trino
        self._kubernetes = kubernetes
        self._desired: dict[SectionName, dict[str, Any]] = {}
        self._plan = catalog_apply.CatalogPlan()

    async def validate(self) -> None:
        """Static validation already happened on every request. Trino validation --
        the ephemeral coordinator -- is a later ticket, so this stage only captures
        the Candidate the rest of the Apply will work from."""
        candidate = await self._candidates.load()
        self._desired = dict(candidate.sections)

        current = await self._snapshots.sections_of(candidate.base_snapshot)
        self._plan = catalog_apply.plan(self._desired.get(SECTION, {}), current.get(SECTION, {}))
        logger.info("planned", extra={"catalogs": self._plan.summary()})

    async def apply(self) -> None:
        """Patch the Secret, then issue the DDL.

        This order is the whole point. A failed DDL leaves a catalog recorded but not
        live -- Apchi knows, because the DDL returned an error, and can retry. The
        reverse order leaves a catalog that works today and silently disappears at
        the next pod restart, possibly weeks later, with nothing connecting the two
        events.

        There is no propagation race: nothing reads the seed mount until the next pod
        start, so the Secret write needs no wait before the DDL.
        """
        await self._kubernetes.write_secret(
            self._settings.catalog_secret_name, render_secret(self._desired)
        )
        logger.info("patched the catalog Secret")

        await catalog_apply.execute(self._trino, self._desired.get(SECTION, {}), self._plan)

    async def verify(self) -> None:
        await verify(
            self._trino,
            self._kubernetes,
            self._desired,
            self._settings.worker_deployment_name,
            self._settings.verification_catalog,
        )

    async def commit(self) -> int | None:
        snapshot = await self._snapshots.commit(self._desired, self._apply_id)
        # The Candidate is re-derived from the new Snapshot, so its diff is empty.
        await self._candidates.reset(base_snapshot=snapshot.number)
        logger.info("committed", extra={"snapshot": snapshot.number})
        return snapshot.number
