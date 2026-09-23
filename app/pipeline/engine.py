"""The real Apply engine.

Walks the Candidate through Validation, Apply, Verification and Commit.

Validation touches nothing but an ephemeral coordinator of its own, which is what
makes it safe to run on its own: the Validate action is this engine's `validate` and
nothing after it.

The ordering that matters most is in `apply`: the Secret is written *before* the DDL
is issued.
"""

import logging
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline import preconditions
from app.pipeline.access_control import deliver as deliver_access_control
from app.pipeline.auto_rollback import declare_incident, restore
from app.pipeline.candidate import CandidateStore
from app.pipeline.maintenance import MaintenanceStore
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.validation import validate_candidate
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
        maintenance: MaintenanceStore,
    ) -> None:
        self._apply_id = apply_id
        self._settings = settings
        self._candidates = candidates
        self._snapshots = snapshots
        self._trino = trino
        self._kubernetes = kubernetes
        self._maintenance = maintenance
        self._desired: dict[SectionName, dict[str, Any]] = {}
        self._plan = catalog_apply.CatalogPlan()

    async def validate(self) -> None:
        """Capture the Candidate, plan the change, then prove it against a real Trino.

        Static validation already ran on every request. What is left is the question
        static checks cannot answer: will Trino accept this? Nothing here reaches the
        Cluster, so a failure leaves it exactly as it was.
        """
        await self._assert_preconditions()

        candidate = await self._candidates.load()
        self._desired = dict(candidate.sections)

        current = await self._snapshots.sections_of(candidate.base_snapshot)
        self._plan = catalog_apply.plan(self._desired.get(SECTION, {}), current.get(SECTION, {}))
        logger.info("planned", extra={"catalogs": self._plan.summary()})

        await validate_candidate(
            self._desired,
            self._plan.created,
            self._trino,
            self._kubernetes,
            self._settings,
            self._apply_id,
        )

    async def _assert_preconditions(self) -> None:
        """Before every Apply, not once at startup: a chart change can reintroduce a
        violation silently, and the resulting failure never looks like its cause."""
        spec = preconditions.pod_spec(
            await self._kubernetes.deployment_pod_spec(self._settings.coordinator_deployment_name)
        )
        preconditions.check(spec, self._settings)

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
        # Re-asserted every Apply rather than written once. It is generated, not
        # Operator-editable, so this is idempotent -- and it means a Cluster whose
        # access-control Secret was changed outside Apchi is corrected by the next Apply
        # instead of quietly keeping catalog DDL open to everyone.
        await deliver_access_control(self._kubernetes, self._settings)

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

    async def roll_back(self) -> None:
        """Put the Cluster back on the latest Snapshot.

        The latest Snapshot is the one this Apply started from, and the pointer never
        moved: no Snapshot is created here. An Apply that failed before any Snapshot
        existed has nothing to go back to, and restoring an empty configuration is the
        right answer -- it undoes exactly what this Apply did.

        Nothing from this Apply's own plan is passed in. The rollback is a diff between
        two durable records, which is what would let it run after an Apchi restart as
        well as inside the Apply that failed.
        """
        latest = await self._snapshots.latest()
        sections = await self._snapshots.sections_of(None if latest is None else latest.number)
        await restore(sections, self._trino, self._kubernetes, self._settings)

    async def declare_incident(self, reason: str) -> None:
        await declare_incident(self._apply_id, reason, self._maintenance, self._settings)

    async def commit(self) -> int | None:
        """Two writes, and the Snapshot goes in first.

        If the second fails the Snapshot still exists and still records a configuration
        that was applied and verified, so it is not rolled back -- what broke was
        MongoDB, and tearing down a healthy Cluster would not fix it. But the Apply that
        produced it is marked failed and never gets to record its number, so the
        Snapshot is left with nothing pointing at it. That is worth an error naming it,
        because it is otherwise only findable by noticing the numbering.
        """
        snapshot = await self._snapshots.commit(self._desired, self._apply_id)
        try:
            # The Candidate is re-derived from the new Snapshot, so its diff is empty.
            await self._candidates.reset(base_snapshot=snapshot.number)
        except Exception:
            logger.error(
                "committed Snapshot %s but could not re-derive the Candidate from it; "
                "the Snapshot is real and is the latest, and this Apply will report "
                "failure without referencing it",
                snapshot.number,
                exc_info=True,
            )
            raise
        logger.info("committed", extra={"snapshot": snapshot.number})
        return snapshot.number
