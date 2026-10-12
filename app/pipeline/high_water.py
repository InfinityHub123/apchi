"""The highest Snapshot number this Cluster has ever committed, kept outside MongoDB.

This exists for exactly one caller: Adoption's precondition. §15 runs Adoption once, over a
Cluster Apchi does not yet manage, and it decides whether a Cluster is already managed by
asking whether MongoDB holds any Snapshots. That question is not quite the right one. An
empty `snapshots` collection is two situations -- a Cluster that was never managed, and one
that is managed by an Apchi whose database happens to be empty right now -- and Adoption's
answer to them differs: configure the first, refuse the second.

Told apart by recording the highest Snapshot number ever committed where an empty database
cannot deny it: a ConfigMap. A ConfigMap and not a Secret, because it holds a number rather
than configuration, and `helm uninstall` leaving it behind is correct for the same reason it
is correct for the Secrets -- it is evidence about a Cluster, not part of a release.

Keeping MongoDB is the platform's job and not Apchi's (§16's division, applied to §18), so
this is not a backup, a second source of truth, or a recovery mechanism. Nothing reads it to
decide what the configuration is. It answers one question for one route: *has this Cluster
committed Snapshots that are not in front of me?*
"""

import logging

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings

logger = logging.getLogger(__name__)

HIGHEST_KEY = "highest_snapshot"


class HighWaterMark:
    """Reads and writes the mark."""

    def __init__(self, kubernetes: KubernetesAdapter, settings: Settings) -> None:
        self._kubernetes = kubernetes
        self._name = settings.high_water_config_map

    async def read(self) -> int | None:
        """The highest Snapshot ever committed here, or None when there is no mark to read.

        None is three situations that cannot be told apart from here and do not need to be:
        a new install, an Apchi that predates this mark, and a Cluster whose mark was
        deleted. All three mean the same thing to the only caller -- there is no evidence
        this Cluster was managed before -- and claiming otherwise without evidence would
        refuse an Adoption that should go ahead.
        """
        data = await self._kubernetes.read_config_map(self._name)
        if HIGHEST_KEY not in data:
            return None
        try:
            return int(data[HIGHEST_KEY])
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
        """Raise the mark to `number`. Never lowers it.

        Lowering would let a Cluster whose database was replaced with an older copy of
        itself look like one that never went further, which is the single thing the mark is
        here to prevent.
        """
        current = await self.read()
        if current is not None and current >= number:
            return
        await self._kubernetes.write_config_map(self._name, {HIGHEST_KEY: str(number)})
        logger.info("high-water mark raised", extra={"snapshot": number})
