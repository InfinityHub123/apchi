"""The Resource Groups Section: the operations the pipeline and the API share.

Rollout-required. Trino builds the resource group manager when the coordinator starts and
never rereads the file, so a change is adopted only by a new pod.
"""

import logging
from dataclasses import dataclass, field

from app.adapters.trino import Trino
from app.api.errors import Conflict, NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorFile,
    Resources,
    SectionPlan,
    ValidationFailure,
)
from app.sections.resource_groups import SECTION, SELECTORS, SETTINGS
from app.sections.resource_groups.generator import (
    MANAGER_PATH,
    RULES_PATH,
    children_of,
    groups_of,
    parent_of,
    render,
    selectors_of,
    settings_of,
)
from app.sections.resource_groups.model import (
    ResourceGroup,
    ResourceGroupSettings,
    ResourceGroupWrite,
    Selector,
    Selectors,
)

logger = logging.getLogger(__name__)


def list_groups(stored: Resources) -> list[ResourceGroup]:
    groups = groups_of(stored)
    return [ResourceGroup(path=path, **groups[path]) for path in sorted(groups)]


def get_group(stored: Resources, path: str) -> ResourceGroup:
    groups = groups_of(stored)
    if path not in groups:
        raise NotFound(f"No resource group at {path!r} in the Configuration Candidate.")
    return ResourceGroup(path=path, **groups[path])


def add_group(stored: Resources, path: str, write: ResourceGroupWrite) -> ResourceGroup:
    """A group is added under a parent that already exists, or as a root.

    The parent check is what keeps the flat map a tree: a path whose parent is missing would
    render as nothing at all, because the tree is rebuilt from the roots down.
    """
    groups = groups_of(stored)
    if path in groups:
        raise Conflict(f"A resource group at {path!r} is already staged.")
    parent = parent_of(path)
    if parent is not None and parent not in groups:
        raise UnprocessablePayload(
            f"There is no resource group at {parent!r} to add {path!r} under.",
            details=[{"property": "path", "problem": f"parent {parent!r} does not exist"}],
        )
    stored[path] = write.model_dump(mode="json")
    return ResourceGroup(path=path, **stored[path])


def edit_group(stored: Resources, path: str, write: ResourceGroupWrite) -> ResourceGroup:
    get_group(stored, path)
    stored[path] = write.model_dump(mode="json")
    return ResourceGroup(path=path, **stored[path])


def delete_group(stored: Resources, path: str) -> None:
    """Refused while the group still has children, rather than orphaning them.

    An orphan renders as nothing -- the tree is built from the roots -- so deleting a parent
    would silently delete its whole subtree. Apchi makes the Operator do it deliberately.
    """
    get_group(stored, path)
    children = children_of(sorted(groups_of(stored)), path)
    if children:
        raise Conflict(
            f"Resource group {path!r} still has subgroups: {', '.join(sorted(children))}. "
            "Remove them first, or they would go with it."
        )
    del stored[path]


def get_settings(stored: Resources) -> ResourceGroupSettings:
    return settings_of(stored)


def set_settings(stored: Resources, settings: ResourceGroupSettings) -> ResourceGroupSettings:
    stored[SETTINGS] = settings.model_dump(mode="json")
    return settings_of(stored)


def get_selectors(stored: Resources) -> Selectors:
    return Selectors(selectors=[Selector.model_validate(s) for s in selectors_of(stored)])


def set_selectors(stored: Resources, selectors: Selectors) -> Selectors:
    """Replaced whole. The list is ordered and first match wins, so a rule cannot be edited
    without seeing what now shadows it."""
    stored[SELECTORS] = [s.model_dump(mode="json") for s in selectors.selectors]
    return get_selectors(stored)


@dataclass
class ResourceGroupsPlan:
    """Which groups moved, and whether the selectors did."""

    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    selectors_changed: bool = False

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed or self.selectors_changed)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"+{len(self.added)}")
        if self.changed:
            parts.append(f"~{len(self.changed)}")
        if self.removed:
            parts.append(f"-{len(self.removed)}")
        if self.selectors_changed:
            parts.append("selectors")
        return " ".join(parts) or "no changes"


class ResourceGroupsSection:
    """The Resource Groups Section as the pipeline sees it."""

    name: SectionName = SECTION
    #: The manager is built once, when the coordinator starts.
    requires_rollout = True

    def coordinator_files(self, settings: Settings) -> tuple[CoordinatorFile, ...]:
        """Two: the rules, and the properties file that makes Trino read them.

        Both in one Secret and one volume, so an empty Section takes them away together.
        Nothing goes in the probe's `config.properties`: unlike the user-mapping file, what
        turns this on is a file of its own.
        """
        return tuple(
            CoordinatorFile(
                secret=settings.resource_groups_secret_name,
                volume=settings.resource_groups_volume_name,
                path=path,
            )
            for path in (RULES_PATH, MANAGER_PATH)
        )

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        return render(desired)

    def plan(self, desired: Resources, current: Resources) -> ResourceGroupsPlan:
        before, after = groups_of(current), groups_of(desired)
        return ResourceGroupsPlan(
            added=[path for path in sorted(after) if path not in before],
            changed=[
                path for path in sorted(after) if path in before and after[path] != before[path]
            ],
            removed=sorted(set(before) - set(after)),
            selectors_changed=selectors_of(desired) != selectors_of(current),
        )

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Nothing beyond the files, which the pipeline has delivered."""

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewriting the files is the whole undo, and the pipeline has done it."""
        return False

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        """The cross-resource checks, over the whole Candidate rather than per request.

        A selector may name a group staged later in the same editing session, so naming a
        group that does not exist *yet* has to be allowed at request time and refused here.

        Trino catches the first of these itself ("Selector refers to nonexistent group"), at
        the cost of a probe that will not start; it does not catch the second at all -- a
        selector pointing at a group with subgroups starts fine and fails queries at
        runtime, which is the kind of failure Validation exists to move earlier.
        """
        groups = groups_of(desired)
        paths = sorted(groups)
        failures: list[ValidationFailure] = []

        # Trino refuses to start on this rather than ignoring it, so catching it here turns
        # a coordinator that will not come back into a Candidate that will not be applied.
        if settings_of(desired).cpu_quota_period is None:
            failures.extend(
                ValidationFailure(
                    section=SECTION,
                    resource=path,
                    reason=(
                        "sets a CPU limit, but no CPU quota period is configured. Trino "
                        "refuses to start without one."
                    ),
                )
                for path in paths
                if groups[path].get("soft_cpu_limit") or groups[path].get("hard_cpu_limit")
            )

        for index, raw in enumerate(selectors_of(desired)):
            target = Selector.model_validate(raw).group
            where = f"{SELECTORS}[{index}]"
            if target not in groups:
                failures.append(
                    ValidationFailure(
                        section=SECTION,
                        resource=where,
                        reason=f"names resource group {target!r}, which is not configured.",
                    )
                )
            elif children_of(paths, target):
                failures.append(
                    ValidationFailure(
                        section=SECTION,
                        resource=where,
                        reason=(
                            f"names resource group {target!r}, which has subgroups. Only a "
                            "group with no subgroups accepts queries."
                        ),
                    )
                )
        return failures

    def needs_probe(self, desired: Resources) -> bool:
        return bool(groups_of(desired) or selectors_of(desired))

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        """Starting is the check: a file Trino cannot parse is a pod that will not start."""
        return []

    async def verify(self, cluster: Cluster, desired: Resources) -> list[str]:
        """Nothing to assert here.

        That a query lands in the group the selectors name is a stronger claim than "the
        Cluster came back", and it is the next ticket's.
        """
        return []
