"""The Permissions Section: the operations the pipeline and the API share.

No Operator-managed grants yet -- what this Section owns today is the file itself and the
block in it that restricts catalog DDL to Apchi. Owning the file is the point: it was
delivered by the pipeline as a special case since slice 1, and a Section that owns it is
what lets grants be added without the pipeline learning anything new.

No Rollout. Trino re-reads the rules on its own `security.refresh-period` timer, so a
permission change costs no queries (section 7.2). What it costs instead is that the change
is not immediate.
"""

import logging

from app.adapters.trino import Trino
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorFile,
    Resources,
    SectionPlan,
    SmokeQuery,
    ValidationFailure,
)
from app.sections.permissions import SECTION
from app.sections.permissions.generator import MOUNT_PATH, render_rules

logger = logging.getLogger(__name__)


class PermissionsPlan:
    """Nothing an Operator can change yet, so nothing to report."""

    @property
    def empty(self) -> bool:
        return True

    def summary(self) -> str:
        return "no changes"


class PermissionsSection:
    """The Permissions Section as the pipeline sees it."""

    name: SectionName = SECTION
    #: Trino re-reads the rules file on a timer. Section 7.2.
    requires_rollout = False

    def coordinator_files(self, settings: Settings) -> tuple[CoordinatorFile, ...]:
        """One file, and the Admin mounts it.

        It must be a whole-volume mount or the kubelet never projects an update, which
        would leave Trino reading the rules Apchi wrote at pod creation and no others
        (section 16). That rules Apchi out as the mounter: Apchi mounts single files.
        """
        return (
            CoordinatorFile(
                secret=settings.access_control_secret_name,
                volume=None,
                path=MOUNT_PATH,
            ),
        )

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        """Always a file, and always the whole of it.

        Re-rendered on every Apply rather than written once, so a Cluster whose
        access-control Secret was changed outside Apchi is corrected by the next Apply
        instead of quietly keeping catalog DDL open to everyone.
        """
        return {MOUNT_PATH: render_rules(settings.trino_user)}

    def plan(self, desired: Resources, current: Resources) -> PermissionsPlan:
        return PermissionsPlan()

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Nothing beyond the file, which the pipeline has delivered."""

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewriting the file is the whole undo, and the pipeline has done it."""
        return False

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        return []

    def needs_probe(self, desired: Resources) -> bool:
        """Never. The probe exists to prove Trino accepts a configuration, and an
        unconfigured Trino allows the DDL the probe is there to issue -- putting these
        rules in front of it would only make the probe refuse Apchi's own statements."""
        return False

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        return []

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Nothing to assert yet.

        Proving the rules are in force means asking the Cluster what an identity can do,
        which is a later ticket. Until then this says nothing rather than something weak.
        """
        return []
