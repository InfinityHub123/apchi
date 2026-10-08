"""The Resource Groups Section: the operations the pipeline and the API share.

Rollout-required. Trino builds the resource group manager when the coordinator starts and
never rereads the file, so a change is adopted only by a new pod.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.adapters.trino import Trino
from app.api.errors import Conflict, NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorFile,
    DiscoveredPaths,
    Parsed,
    ParseProblem,
    Resources,
    SectionPlan,
    SmokeQuery,
    Unaccounted,
    ValidationFailure,
)
from app.sections.resource_groups import SECTION, SELECTORS, SEPARATOR, SETTINGS
from app.sections.resource_groups.generator import (
    MANAGER_FILE,
    MANAGER_PATH,
    RULES_PATH,
    Unreadable,
    children_of,
    groups_of,
    parent_of,
    parse_rules,
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
from app.sections.resource_groups.selectors import Unpredictable, group_for

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

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """`resource-groups.config-file` names the rules, and the properties file that names
        it is at a fixed path of its own.

        Both are read, because a mismatch between them is worth reporting: Trino reads
        whatever `config-file` points at, so a properties file naming something other than
        the rules Apchi found means the rules Apchi found are not the rules in force.
        """
        named = properties.get("resource-groups.config-file")
        if named:
            return DiscoveredPaths(files={RULES_PATH: named, MANAGER_PATH: MANAGER_PATH})
        return DiscoveredPaths(
            files={RULES_PATH: RULES_PATH, MANAGER_PATH: MANAGER_PATH},
            why=(
                "no resource-groups.config-file property was found, so this is where Apchi "
                "would put the rules rather than where the Cluster says they are"
            ),
        )

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """Both files absent means no Resource Groups, which is how this Section says so.

        The rules file is the configuration; the properties file only points at it. So the
        rules being absent is "nothing configured" whatever the properties file says, and a
        properties file pointing somewhere else is reported rather than followed -- Apchi
        reading a file it does not own would be reading the Admin's configuration as if it
        were an Operator's.
        """
        rules = files.get(RULES_PATH)
        manager = files.get(MANAGER_PATH)
        if rules is None:
            if manager is None:
                return Parsed()
            return Parsed(
                unaccounted=(
                    Unaccounted(
                        path=MANAGER_PATH,
                        what="Trino is told to read a resource groups file that is not there",
                        content=manager,
                    ),
                )
            )
        try:
            resources, unaccounted = parse_rules(RULES_PATH, rules)
        except Unreadable as exc:
            raise ParseProblem(exc.path, exc.reason) from exc

        found = tuple(
            Unaccounted(path=RULES_PATH, what=what, content=content)
            for what, content in unaccounted
        )
        if manager is not None and manager != MANAGER_FILE:
            # Trino reads whatever config-file names, so a properties file that is not the
            # one Apchi writes means the rules above may not be the rules in force.
            found += (
                Unaccounted(
                    path=MANAGER_PATH,
                    what="the properties file is not the one Apchi writes",
                    content=manager,
                ),
            )
        return Parsed(resources=resources, unaccounted=found)

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

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Did the query Verification just ran land where the selectors say it should?

        The first functional check a file-based Section has been able to have. An Event
        Listener's output goes to a sink Apchi cannot read and a mapping pattern would need
        a certificate Apchi does not hold, but Trino records the resource group a query ran
        in -- so the smoke query is evidence, and this asks the Cluster what happened rather
        than reading configuration back (§8).

        Three ways this declines to judge rather than inventing a failure: no selectors, so
        nothing claims where anything goes; a prediction Apchi cannot make with certainty;
        and a Cluster that reports no group at all, which is not something Trino does while
        a manager is loaded.
        """
        if not selectors_of(desired):
            return []
        try:
            expected = group_for(
                selectors_of(desired),
                user=smoke.user,
                source=smoke.source,
                query=smoke.sql,
            )
        except Unpredictable as exc:
            logger.info("resource group not verified", extra={"why": str(exc)})
            return []
        if expected is None:
            # No selector claims this query, so there is nothing for the Cluster to
            # contradict. Where Trino puts it instead is Trino's business.
            return []

        reached = await _group_of(cluster, smoke.query_id)
        if reached is None:
            logger.info("resource group not verified", extra={"why": "the Cluster reported none"})
            return []
        if reached != expected:
            return [
                f"The verification query ran in resource group {reached!r}, but the "
                f"selectors put it in {expected!r}. The coordinator is not running the "
                "resource group configuration this Apply delivered."
            ]
        logger.info("resource group verified", extra={"group": expected})
        return []


async def _group_of(cluster: Cluster, query_id: str) -> str | None:
    """The group Trino filed a query under, as a dotted path.

    `system.runtime.queries.resource_group_id` is the path in segments -- `['global','etl']`
    -- so joining it is what makes it comparable with the paths the Candidate is keyed by.
    """
    rows = await cluster.trino.query(
        "SELECT resource_group_id FROM system.runtime.queries "
        f"WHERE query_id = {_literal(query_id)}"
    )
    if not rows or not rows[0][0]:
        return None
    segments: list[str] = list(rows[0][0])
    return SEPARATOR.join(segments)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
