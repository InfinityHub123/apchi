"""The high-water mark: the one thing Apchi keeps outside MongoDB.

§18 names the disaster the design creates -- Mongo holds the only copy of every Snapshot, and
if it is lost the Cluster keeps running a configuration nobody can reproduce. The first thing
wrong with that is not the loss. It is that **Apchi cannot tell it happened**. Measured against
an empty database, Apchi reports itself healthy, `/snapshots` returns `[]` and `/candidate`
returns 404, and none of that distinguishes a fresh install from a catastrophic loss -- while
the safe action in each case is the opposite one.

The irreversible consequence is in Adoption. It refuses only when Snapshots exist (§15), so
after a loss there are none, and the obvious thing an Operator would try runs happily, mints a
new Snapshot 1, and destroys the evidence that a backup was worth restoring.

So Apchi records the highest Snapshot number it has ever committed where MongoDB being gone
cannot take it: a ConfigMap. A ConfigMap and not a Secret, because it holds a number rather
than configuration, and `helm uninstall` leaving it behind is correct for the same reason it is
correct for the Secrets -- it is evidence about a Cluster, not part of a release.

This is not a second source of truth. It answers one question -- *was there ever more than
this?* -- and nothing reads it to decide what the configuration is.
"""

import logging
import uuid

from pydantic import BaseModel, Field

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings

logger = logging.getLogger(__name__)

HIGHEST_KEY = "highest_snapshot"
CLUSTER_KEY = "cluster_id"


class HighWater(BaseModel):
    """What Apchi has ever committed here, as recorded outside MongoDB."""

    highest_snapshot: int = Field(description="The highest Snapshot number ever committed.")
    cluster_id: str = Field(
        description=(
            "Minted once, when the mark is first written. Identifies the Apchi install "
            "whose history this is, so a backup can be refused when it belongs to another."
        )
    )


class History(BaseModel):
    """Whether Apchi's memory is all there, and what is missing if not."""

    intact: bool
    held: int | None = Field(description="The highest Snapshot MongoDB currently holds.")
    ever: int | None = Field(
        default=None, description="The highest Snapshot ever committed, per the mark."
    )

    @property
    def lost(self) -> bool:
        return not self.intact

    def __str__(self) -> str:
        if self.intact:
            return "intact"
        return (
            f"MongoDB holds {'no Snapshots' if self.held is None else f'Snapshot {self.held}'} "
            f"and this Cluster committed {self.ever}"
        )


class HighWaterMark:
    """Reads and writes the mark. Tolerates a Kubernetes that will not answer."""

    def __init__(self, kubernetes: KubernetesAdapter, settings: Settings) -> None:
        self._kubernetes = kubernetes
        self._name = settings.high_water_config_map

    async def read(self) -> HighWater | None:
        """The mark, or None when there is not one to read.

        None is three situations that cannot be told apart from here and do not need to be:
        a new install, an Apchi that predates this mark, and a Cluster whose mark was
        deleted. All three mean the same thing -- there is no evidence of lost history --
        and claiming a disaster without evidence is worse than staying quiet.
        """
        data = await self._kubernetes.read_config_map(self._name)
        if HIGHEST_KEY not in data:
            return None
        try:
            return HighWater(
                highest_snapshot=int(data[HIGHEST_KEY]),
                cluster_id=data.get(CLUSTER_KEY, ""),
            )
        except ValueError:
            # Somebody edited it by hand. Refusing to parse is right, and so is not
            # crashing: the mark is evidence, and Apchi serves requests without it.
            logger.error(
                "the high-water ConfigMap %s holds %r, which is not a Snapshot number",
                self._name,
                data[HIGHEST_KEY],
            )
            return None

    async def record(self, number: int) -> None:
        """Raise the mark to `number`, minting the Cluster id on first write.

        Never lowers it. The mark's whole value is that it remembers a Snapshot MongoDB has
        forgotten, so a restore from an old backup -- which legitimately leaves MongoDB
        behind -- must not quietly agree that the newer Snapshots never existed.
        """
        current = await self.read()
        if current is not None and current.highest_snapshot >= number:
            return
        cluster_id = current.cluster_id if current and current.cluster_id else uuid.uuid4().hex
        await self._kubernetes.write_config_map(
            self._name, {HIGHEST_KEY: str(number), CLUSTER_KEY: cluster_id}
        )
        logger.info("high-water mark raised", extra={"snapshot": number})

    async def history(self, held: int | None) -> History:
        """Whether `held` -- the highest Snapshot MongoDB has -- is the whole history.

        An unreadable mark reports intact rather than lost, and deliberately: Apchi tolerates
        an unreachable Kubernetes at startup by design, and a transient 403 or a connection
        refused must not be reported as the loss of every Snapshot.
        """
        try:
            mark = await self.read()
        except Exception:
            logger.warning("could not read the high-water mark", exc_info=True)
            return History(intact=True, held=held)
        if mark is None:
            return History(intact=True, held=held)
        intact = held is not None and held >= mark.highest_snapshot
        return History(intact=intact, held=held, ever=mark.highest_snapshot)
