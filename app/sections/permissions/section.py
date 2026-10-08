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
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.adapters.trino import Trino
from app.api.errors import Conflict, NotFound
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorDirectory,
    Delivery,
    DiscoveredPaths,
    Parsed,
    ParseProblem,
    Resources,
    SectionPlan,
    SmokeQuery,
    ValidationFailure,
)
from app.sections.permissions import SECTION
from app.sections.permissions.generator import (
    MOUNT_DIR,
    MOUNT_PATH,
    PROBE_PROPERTIES,
    PROPERTIES_PATH,
    Unreadable,
    parse_rules,
    render_rules,
)
from app.sections.permissions.model import (
    Grant,
    GrantWrite,
    Privilege,
    SystemRule,
    SystemRules,
    key_of,
)

logger = logging.getLogger(__name__)


def system_rules(admin: AdminValues) -> SystemRules:
    """What Apchi owns in this file, and why -- as it stands right now.

    The last of these changes with the posture, because what it says stops being true: an
    Operator reading "everything no grant names is allowed" on a Cluster where grants are
    enforced would be reading a lie.
    """
    return SystemRules(
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
                rule="Everyone may run queries; only you may see yours.",
                why=(
                    "By default any authenticated End User can view and kill any query, and query "
                    "text routinely contains data. This block closes that. It is all-or-nothing: "
                    "once it exists, anything it does not match is denied, including the right to "
                    "run a query at all -- so the rule letting everyone execute is what keeps the "
                    "Cluster serving, and widening this block carelessly stops queries rather "
                    "than leaking data. Seeing your own queries needs no rule: Trino gives you "
                    "those whatever the rules say. Killing your own does, so Apchi writes one per "
                    "identity it knows about -- the identities named in a grant."
                ),
            ),
            SystemRule(
                rule="Anyone may call system.runtime.kill_query.",
                why=(
                    "A file-based access control denies procedure execution unless a rule "
                    "allows it, so Apchi's own file took this away from every End User the "
                    "day it was installed -- including from somebody killing their own "
                    "query. Granting the procedure does not decide whose query may be "
                    "killed: the rule above still does that. No other procedure is granted."
                ),
            ),
            SystemRule(
                rule="Apchi may see everyone's queries.",
                why=(
                    "The running-query count Review shows before a Rollout reads "
                    "system.runtime.queries, and Trino filters those rows by who may view them. "
                    "Without this Apchi would count only its own queries and report that a "
                    "Rollout destroys nothing."
                ),
            ),
            _POSTURE[admin.enforce_permissions],
        ]
    )


#: The last system-owned rule: the posture an Admin has chosen.
_POSTURE = {
    False: SystemRule(
        rule="Everything no grant names is allowed, for everyone.",
        why=(
            "What the Cluster did before Apchi wrote a tables block at all. Staging a grant "
            "records intent; it does not revoke anyone's access. An Admin removes this when "
            "the grants are complete, and only an Admin can: on a Cluster already serving "
            "users, removing it denies every identity without a grant everything it had."
        ),
    ),
    True: SystemRule(
        rule="An identity may reach only what it has been granted.",
        why=(
            "An Admin has decided the grants are complete and taken the catch-all away, so a "
            "table no grant names is denied to everyone but Apchi. Apchi keeps its own read "
            "access through its own rule, which is what leaves it able to verify and to "
            "recover."
        ),
    ),
}


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

    def coordinator_files(self, settings: Settings) -> tuple[Delivery, ...]:
        """One file, and the Admin mounts it.

        It must be a whole-volume mount or the kubelet never projects an update, which
        would leave Trino reading the rules Apchi wrote at pod creation and no others
        (section 16). That rules Apchi out as the mounter: Apchi mounts single files.
        """
        return (
            CoordinatorDirectory(
                secret=settings.access_control_secret_name,
                path=MOUNT_DIR,
                probe_files={PROPERTIES_PATH: PROBE_PROPERTIES},
            ),
        )

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        """Always a file, and always the whole of it.

        Re-rendered on every Apply rather than written once, so a Cluster whose
        access-control Secret was changed outside Apchi is corrected by the next Apply
        instead of quietly keeping catalog DDL open to everyone.

        Which is exactly why the preserved rules have to be passed in here. Rendering
        without them would rewrite the file without them, and an adopted Cluster would lose
        on its first Apply every rule Adoption had carefully kept (§15). They are Admin
        values rather than Candidate resources, so they survive a rollback and leave with an
        Admin's decision and nothing else (invariant 9).
        """
        return {
            MOUNT_PATH: render_rules(
                settings.trino_user,
                desired,
                settings.verification_catalog,
                admin.enforce_permissions,
                admin.preserved_access_control,
            )
        }

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """`security.config-file` names the rules, and Trino reads whatever it names.

        There is no default worth falling back to: without that property Trino has no
        file-based access control at all, so a Cluster that does not set it has no rules to
        discover rather than rules somewhere Apchi should guess at.
        """
        named = properties.get("security.config-file")
        if named:
            return DiscoveredPaths(files={MOUNT_PATH: named})
        return DiscoveredPaths(
            files={MOUNT_PATH: MOUNT_PATH},
            why=(
                "no security.config-file property was found, so this Cluster may have no "
                "file-based access control at all -- this is where Apchi would put the rules"
            ),
        )

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """Grants where Apchi's model reaches, Admin values where it does not.

        The split is the point. Apchi's grants are deliberately smaller than Trino's file
        (§13.4), so a Cluster onboarded years into its life has rules with no grant to
        become -- and the answer is to keep them working beneath the grants rather than drop
        them or refuse the Cluster (§13.3's pattern, decided in #82).
        """
        content = files.get(MOUNT_PATH)
        if content is None:
            return Parsed()
        try:
            grants, preserved, enforced = parse_rules(
                MOUNT_PATH, content, settings.trino_user, settings.verification_catalog
            )
        except Unreadable as exc:
            raise ParseProblem(exc.path, exc.reason) from exc
        return Parsed(
            resources=grants,
            admin={"preserved_access_control": preserved, "enforce_permissions": enforced},
        )

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
        """Whenever an Operator has granted something.

        This was once never, on the grounds that Apchi's own rules would make the probe
        refuse Apchi's own statements. That stopped being true when the file grew the
        reserved rules: a coordinator running what Apchi generates accepts every statement
        the probe issues, verified against a real one.

        What it buys is the failure a running Cluster hides. Trino keeps the old rules when
        a refresh fails, so a malformed file changes nothing today and stops the coordinator
        starting whenever it next restarts -- hours or weeks later, with nothing connecting
        the two events. A Candidate with no grants is not worth a pod: the file is then
        entirely generated and constant.
        """
        return bool(desired)

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        """Starting is the check: a rules file Trino will not parse is a pod that will not
        start, which the pipeline turns into a failure."""
        return []

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Nothing to assert yet.

        Proving the rules are in force means asking the Cluster what an identity can do,
        which is a later ticket. Until then this says nothing rather than something weak.
        """
        return []
