"""The real Apply engine.

Walks the Candidate through Validation, Apply, Verification and Commit.

Validation touches nothing but an ephemeral coordinator of its own, which is what
makes it safe to run on its own: the Validate action is this engine's `validate` and
nothing after it.

The ordering that matters most is in `apply`: the Secret is written *before* the DDL
is issued.
"""

import logging

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline import preconditions
from app.pipeline.admin_values import AdminStore
from app.pipeline.auto_rollback import declare_incident, restore
from app.pipeline.candidate import CandidateStore
from app.pipeline.files import deliver
from app.pipeline.impact import sections_needing_rollout
from app.pipeline.maintenance import MaintenanceStore
from app.pipeline.rollout import roll_out
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.validation import validate_candidate
from app.pipeline.verification import verify
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan
from app.sections.registry import REGISTERED

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
        admin: AdminStore,
    ) -> None:
        self._apply_id = apply_id
        self._settings = settings
        self._candidates = candidates
        self._snapshots = snapshots
        self._trino = trino
        self._kubernetes = kubernetes
        self._maintenance = maintenance
        self._admin = admin
        self._desired: dict[SectionName, Resources] = {}
        self._plans: dict[SectionName, SectionPlan] = {}
        self._rollout: set[SectionName] = set()
        self._cluster = Cluster(trino=trino, kubernetes=kubernetes, settings=settings)

    async def _freeze_admin_values(self) -> None:
        """Read the Admin values once and hold them for the rest of this Apply.

        Loaded here rather than in the constructor because it is I/O, and rebuilt into the
        Cluster because that is what every Section renders against.
        """
        self._cluster = Cluster(
            trino=self._trino,
            kubernetes=self._kubernetes,
            settings=self._settings,
            admin=await self._admin.load(),
        )

    async def validate(self) -> None:
        """Capture the Candidate, plan the change, then prove it against a real Trino.

        Static validation already ran on every request. What is left is the question
        static checks cannot answer: will Trino accept this? Nothing here reaches the
        Cluster, so a failure leaves it exactly as it was.
        """
        await self._assert_preconditions()
        await self._freeze_admin_values()

        self._desired, current = await self._desired_and_baseline()
        self._plans = {
            section.name: section.plan(
                self._desired.get(section.name, {}), current.get(section.name, {})
            )
            for section in REGISTERED
        }
        logger.info(
            "planned",
            extra={name: plan.summary() for name, plan in self._plans.items()},
        )

        self._rollout = await sections_needing_rollout(self._plans, self._desired, self._cluster)
        await validate_candidate(self._desired, self._plans, self._cluster, self._apply_id)

    async def _desired_and_baseline(
        self,
    ) -> tuple[dict[SectionName, Resources], dict[SectionName, Resources]]:
        """What this Apply delivers, and what it is a change from.

        An Operator Apply delivers the Candidate, measured against the Snapshot it was
        derived from.
        """
        candidate = await self._candidates.load()
        return dict(candidate.sections), await self._snapshots.sections_of(candidate.base_snapshot)

    async def _assert_preconditions(self) -> None:
        """Before every Apply, not once at startup: a chart change can reintroduce a
        violation silently, and the resulting failure never looks like its cause."""
        spec = preconditions.pod_spec(
            await self._kubernetes.deployment_pod_spec(self._settings.coordinator_deployment_name)
        )
        await preconditions.check(self._kubernetes, spec, self._settings)

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
        for section in REGISTERED:
            desired = self._desired.get(section.name, {})
            # The file first, then whatever else the Section does. A Section that only owns
            # a file does nothing here at all.
            await deliver(section, self._cluster, desired)
            await section.apply(self._cluster, desired, self._plans[section.name])

    def rollout_needed(self) -> bool:
        """True when a Section Trino adopts only by restarting actually changed.

        Both halves matter. A Section that needs no restart never causes one, and a
        rollout-required Section that did not change does not either -- otherwise every
        Apply on a Cluster with an Event Listener configured would terminate every running
        query for nothing.
        """
        return bool(self._rollout)

    async def roll_out(self) -> None:
        changed = sorted(self._rollout)
        await roll_out(
            self._kubernetes,
            self._settings.coordinator_deployment_name,
            f"apply {self._apply_id} changed {', '.join(changed)}",
            self._settings.rollout_timeout_seconds,
        )

    async def verify(self) -> None:
        await verify(
            self._cluster,
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
        await self._freeze_admin_values()
        latest = await self._snapshots.latest()
        sections = await self._snapshots.sections_of(None if latest is None else latest.number)
        await restore(sections, self._cluster)

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


class AdminEngine(Engine):
    """An Apply that carries an Admin change and creates no Snapshot.

    Two differences from an Operator Apply, and both follow from what Admin values are.

    It delivers the **latest Snapshot**, not the Candidate. Invariant 2 says what ran on a
    Cluster was the Snapshot merged with the Admin values current at the time, and that is
    exactly what this rebuilds with today's values. Delivering the Candidate instead would
    push an Operator's staged, unreviewed changes to the Cluster on an Admin's authority.

    It commits nothing. Snapshots are the history of Operator-managed configuration, and
    this Apply changed none of it (invariant 9, §14). The Apply record is the history of
    what the Admin did; the Snapshot pointer does not move.

    Everything else is the same pipeline -- validated on the ephemeral coordinator, rolled
    out, verified, rolled back on failure -- because arbitrary low-level values are
    precisely the class of configuration most able to stop Trino booting, and an Admin
    fixing something during an incident cannot be made to wait for an Operator.
    """

    async def _desired_and_baseline(
        self,
    ) -> tuple[dict[SectionName, Resources], dict[SectionName, Resources]]:
        latest = await self._snapshots.latest()
        sections = await self._snapshots.sections_of(None if latest is None else latest.number)
        # The same on both sides: nothing an Operator owns is changing, so every Section's
        # plan is empty and the Rollout decision comes from the Cluster instead.
        return dict(sections), sections

    async def commit(self) -> int | None:
        logger.info("admin apply committed no snapshot")
        return None
