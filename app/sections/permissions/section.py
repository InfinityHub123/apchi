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
from dataclasses import dataclass, field

from app.adapters.trino import Trino
from app.api.errors import Conflict, NotFound
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
from app.sections.permissions.model import (
    Grant,
    GrantWrite,
    Privilege,
    SystemRule,
    SystemRules,
    key_of,
)

logger = logging.getLogger(__name__)


SYSTEM_RULES = SystemRules(
    rules=[
        SystemRule(
            rule="Only Apchi may create or drop a catalog.",
            why=(
                "Catalogs are applied as DDL, and Apchi is the only identity with the owner "
                "access mode that CREATE CATALOG needs. Without it an End User could create a "
                "catalog Apchi does not know about, which the next Apply would then remove."
            ),
        ),
        SystemRule(
            rule="Apchi may read the verification catalog.",
            why=(
                "A tables block denies every table it does not match, to everyone. "
                "Verification's smoke query, the running-query count and the resource group "
                "read-back all read tables there, so without this rule an Apply would pass "
                "only by luck of the catch-all, and removing that catch-all would cut Apchi "
                "off from the Cluster it has to recover."
            ),
        ),
        SystemRule(
            rule="Everything no grant names is allowed, for everyone.",
            why=(
                "What the Cluster did before Apchi wrote a tables block at all. Staging a "
                "grant records intent; it does not revoke anyone's access. Narrowing this is "
                "a separate, deliberate decision."
            ),
        ),
    ]
)


def list_grants(stored: Resources) -> list[Grant]:
    return [Grant(key=key, **stored[key]) for key in sorted(stored)]


def get_grant(stored: Resources, key: str) -> Grant:
    if key not in stored:
        raise NotFound(f"No grant {key!r} in the Configuration Candidate.")
    return Grant(key=key, **stored[key])


def add_grant(stored: Resources, write: GrantWrite) -> Grant:
    """One grant per identity and place. Granting again is an edit, not a second rule.

    Two rules for the same place would both be in the file, and first match wins -- so the
    second would be dead weight an Operator could edit forever with no effect.
    """
    key = key_of(write)
    if key in stored:
        raise Conflict(
            f"A grant for {write.identity!r} on that resource is already staged as "
            f"{key!r}. Change it rather than adding a second."
        )
    stored[key] = write.model_dump(mode="json", by_alias=True)
    return Grant(key=key, **stored[key])


def set_privileges(stored: Resources, key: str, privileges: list[Privilege]) -> Grant:
    """What a grant can be changed to. The place it applies to is its identity: changing
    that is removing one grant and adding another."""
    get_grant(stored, key)
    stored[key]["privileges"] = list(privileges)
    return Grant(key=key, **stored[key])


def delete_grant(stored: Resources, key: str) -> None:
    get_grant(stored, key)
    del stored[key]


@dataclass
class PermissionsPlan:
    """Which grants moved."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.changed:
            parts.append(f"~{len(self.changed)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        return " ".join(parts) or "no changes"


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
        return {
            MOUNT_PATH: render_rules(settings.trino_user, desired, settings.verification_catalog)
        }

    def plan(self, desired: Resources, current: Resources) -> PermissionsPlan:
        return PermissionsPlan(
            added=[key for key in sorted(desired) if key not in current],
            changed=[
                key for key in sorted(desired) if key in current and desired[key] != current[key]
            ],
            removed=sorted(set(current) - set(desired)),
        )

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
