"""What a Section is.

A Section provides exactly four things to the pipeline: a model, a configuration
generator, an apply strategy, and whether it requires a restart. This module is that
contract, written from how the pipeline actually uses it -- there are no hooks here that
nothing calls.

The dependency runs one way. The pipeline consumes Sections; a Section knows nothing
about the pipeline, which is why the failures below are returned as data rather than
raised: the pipeline decides what a failure means to the Apply it is running.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol, TypeGuard

from pydantic import BaseModel

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues

#: One Section's resources as they are stored in the Candidate and in Snapshots, keyed
#: by resource name. Deliberately untyped here: the shape belongs to the Section's own
#: model, and the pipeline never looks inside it.
Resources = dict[str, Any]


class ParseProblem(Exception):
    """A file Apchi cannot read at all.

    Distinct from something in a file Apchi has no vocabulary for, which is `Unaccounted`
    and is a report rather than a failure. This is the file being unreadable: not JSON, not
    the shape the Section generates, a value where a list belongs. Carries the path, because
    "could not parse" without one sends an Admin reading the wrong file.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class Unaccounted:
    """Something a Section read and cannot express.

    Apchi's models are deliberately smaller than Trino's file formats, so a file an Admin
    wrote by hand will contain things no Section resource can hold -- a rule type Apchi does
    not model, a property it does not curate, a second pattern where it has room for one.

    Reported rather than dropped, because dropping is the one outcome that must not happen:
    Adoption would import a configuration missing a piece, and the first Apply would then
    delete that piece from the Cluster. What becomes of these -- preserved as an Admin value
    or refused -- is §15's decision and not a Section's.

    `content` is the thing itself, so whatever preserves it later does not have to parse the
    file a second time to find it.
    """

    path: str
    what: str
    content: Any = None


@dataclass(frozen=True)
class Parsed:
    """What a Section read out of the Cluster's files."""

    resources: Resources = field(default_factory=dict)
    unaccounted: tuple[Unaccounted, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether everything in the files is something Apchi can hold."""
        return not self.unaccounted


class ValidationFailure(BaseModel):
    """One reason a Candidate should not be applied, named well enough for an
    Operator to know what to fix."""

    section: SectionName | None = None
    resource: str | None = None
    reason: str

    def __str__(self) -> str:
        where = "/".join(part for part in (self.section, self.resource) if part)
        return f"{where}: {self.reason}" if where else self.reason


@dataclass(frozen=True)
class CoordinatorDirectory:
    """A directory of files Apchi fills and the **Admin** mounts.

    The other half of the dichotomy, and the shape of every file Apchi cannot mount itself.
    The access-control rules have to be a whole-volume mount or the kubelet never projects
    an update (section 16); the client certificate directory has to be one or a certificate
    added after the pod started never appears (ADR-0005). In both cases the mount is part of
    the deployment, and in both cases what is *in* the directory changes while the pod
    stays -- which is exactly what a subPath mount cannot do.

    So Apchi owns the Secret's contents and nothing else. It never adds, removes or reasons
    about the mount; it only insists, through the preconditions, that nobody mounts this
    Secret with subPath. How many files are in here is the Section's business and may change
    with the Candidate, which is the other reason this is a directory rather than a list of
    files: a Section with a certificate per Operator upload cannot declare them in advance.
    """

    secret: str
    #: The directory the Admin mounts the Secret at. Every file a Section renders must be
    #: directly inside it.
    path: str
    #: Files the validation probe needs beside this one, which on the Cluster are the
    #: Admin's. A file Trino was not told to read is a file Trino never rejects, so a probe
    #: that holds the content without the configuration pointing at it proves nothing.
    probe_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SmokeQuery:
    """The query Verification ran against the Cluster, and how to find it again.

    Handed to every Section so that a Section with something functional to prove can prove
    it about a query that actually ran, rather than issuing one of its own. Trino records
    what became of a query by id, which is what makes this evidence rather than a claim.
    """

    sql: str
    query_id: str
    user: str
    source: str


@dataclass(frozen=True)
class CoordinatorFile:
    """One file, at one path, mounted by Apchi.

    Owning the mount is not a detail. Trino refuses to start when a file it was told to
    read is missing, and Kubernetes turns a subPath mount of an absent Secret key into a
    *directory* it dies on -- so "this Section is empty" can only be expressed by the mount
    not being there, which means Apchi adds and removes it on the Admin's pod template.
    Everything else on that template belongs to the Admin, and a precondition rejects
    anything of theirs mounted at a path declared here.

    Mounted with subPath, deliberately: Apchi replaces the mount when the file changes, and
    the Rollout that follows is what makes the new content live. Nothing has to reach a pod
    that is staying.
    """

    secret: str
    volume: str
    path: str
    #: Configuration the validation probe needs before it will read this file at all. The
    #: Cluster's own copy of these properties belongs to the Admin; the probe is Apchi's,
    #: so Apchi sets them there itself.
    probe_config: Mapping[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """The Secret key, which is the filename. Derived rather than declared: two names
        for one thing is one of them waiting to be wrong."""
        return PurePosixPath(self.path).name


#: What a Section declares about where its files go: single files Apchi mounts, and
#: directories the Admin mounts. Nothing else; a Section that needed a third shape would be
#: telling us something about Trino we do not yet know.
Delivery = CoordinatorFile | CoordinatorDirectory


@dataclass(frozen=True)
class Cluster:
    """Everything a Section is allowed to touch.

    Passed as one object so that adding a capability a Section needs does not change
    every Section's signature.
    """

    trino: Trino
    kubernetes: KubernetesAdapter
    settings: Settings
    #: The Admin values in force, read once at the start of an operation and held for the
    #: whole of it -- an Admin editing them mid-Apply must not change what that Apply is
    #: delivering, for the reason invariant 4 freezes the Candidate.
    admin: AdminValues = field(default_factory=AdminValues)


class SectionPlan(Protocol):
    """What a Section would do to the Cluster. Computed before anything is issued, so
    it can be reported and logged before the Cluster changes."""

    @property
    def empty(self) -> bool: ...

    def summary(self) -> str: ...


@dataclass(frozen=True)
class DiscoveredPaths:
    """Where a Section's configuration is, mapped to where Apchi would keep it.

    Both halves are needed and they are usually different, which is the whole difficulty of
    Adoption. The **key** is the path Apchi writes and `parse_files` expects; the **value**
    is where that file actually is on this Cluster. A parser handed content under the Admin's
    path would look up Apchi's path, find nothing, and report a Section with no configuration
    -- which is the one failure Adoption must never produce.

    `files` are read individually. `directories` are listed, because for some Sections the
    question is "what is in here" rather than "what is at this path": client certificates are
    a directory whose contents *are* the configuration.

    `why` explains a path Apchi guessed rather than read from the Cluster, so a discovery can
    say "I looked here because this is where Apchi would put it, not because your Cluster
    told me" -- and an empty result is not mistaken for a fact.
    """

    files: Mapping[str, str] = field(default_factory=dict)
    directories: Mapping[str, str] = field(default_factory=dict)
    why: str | None = None

    @property
    def everywhere_looked(self) -> list[str]:
        """The actual paths, which is what an Operator wants to see."""
        return sorted({*self.files.values(), *self.directories.values()})


class ParsesFiles(Protocol):
    """A Section that can read its own files back: the inverse of `render_files`.

    Deliberately separate from `Section` rather than part of it, because it is not yet true
    of every Section. Catalogs and Permissions each have a problem of their own -- a
    catalog's properties cannot be read out of Trino at all, and a hand-written rules file
    says more than Apchi's grants can express -- and until those are solved the type system
    should say which Sections can be parsed rather than let a stub claim they all can.

    Nothing in Apchi read Trino configuration before Adoption (§15), and the reason it has
    to now is that Trino cannot be asked: a catalog's properties, a rules file, a selector
    list -- none of them can be read back out of a running coordinator, so the only place a
    Cluster's configuration can be recovered from is the files themselves.
    """

    name: SectionName

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        """The other direction. Part of this protocol rather than only of `Section` because
        the round trip is what makes a parser trustworthy: Adoption regenerates what it
        parsed and compares, since a parse that silently drops a field would have that field
        deleted from the Cluster by the first Apply."""
        ...

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """Where this Section's configuration is on a Cluster Apchi has not configured.

        `render_files` declares where Apchi *puts* its files. This answers the different
        question Adoption asks: where are they **now**, on a Cluster whose files an Admin
        placed and named. Usually somewhere else, and the only authority on where is Trino's
        own configuration -- `security.config-file` says where the rules are,
        `resource-groups.config-file` says where the resource groups are.

        `properties` is the coordinator's configuration as Apchi managed to read it, merged
        across the property files it found. A Section finds its own path in there and falls
        back to where Apchi would put it, because a Cluster part-way through onboarding has
        some of each.
        """
        ...

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """`files` is keyed by the paths this Section declares, and a path the Cluster does
        not have is simply absent -- a legitimate state, not an error, since half the
        Sections express "nothing configured" as the absence of their file.

        Raises `ParseProblem` for a file it cannot read. Returns `Unaccounted` for anything
        it read and cannot hold: the difference matters, because the first is an Admin's
        file being wrong and the second is Apchi's model being smaller than Trino's.
        """
        ...


class Section(Protocol):
    """One managed area of a Configuration Candidate."""

    name: SectionName

    #: Whether Trino adopts this Section only by restarting. A property of how Trino
    #: consumes the configuration, never of when an Operator's edit takes effect.
    requires_rollout: bool

    def coordinator_files(self, settings: Settings) -> tuple[Delivery, ...]:
        """The files this Section delivers to the coordinator. Empty when it delivers none.

        Declaring them is all a Section does about delivery: the pipeline writes the Secret,
        mounts each file when there is content for it, unmounts it when there is not, puts
        them in front of the validation probe, and tells the preconditions to guard the
        paths.

        More than one because a Section may need Trino *told* to read its file. Resource
        Groups is the case: the JSON is inert until `resource-groups.properties` points at
        it, so the Section that owns the one owns the other, and an empty Section takes both
        away rather than leaving Trino pointed at a file that is no longer there.
        """
        ...

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        """What belongs at each declared path. A path left out is a path unmounted.

        Admin values are passed alongside the Candidate's resources rather than merged into
        them, because the two have different lifecycles: what is rendered is the pair, and
        only the Candidate half is ever recorded in a Snapshot (§14).
        """
        ...

    def plan(self, desired: Resources, current: Resources) -> SectionPlan:
        """What applying `desired` over `current` would do."""
        ...

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Make the Cluster match `desired`. May leave the Cluster partly changed if it
        raises; the pipeline's Auto Rollback is what handles that."""
        ...

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Put the Cluster back to a Snapshot's configuration, without assuming how far a
        failed Apply got.

        Returns whether anything actually changed, which is what decides whether the
        rollback has to restart Trino. It cannot be taken from the failed Apply's plan:
        recovery after a restart has no plan, because the process that made it is gone.
        """
        ...

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        """Checks that need no ephemeral coordinator. Run first, so a Candidate that
        cannot possibly work is rejected without paying for a pod."""
        ...

    def needs_probe(self, desired: Resources) -> bool:
        """Whether this Section has anything for an ephemeral coordinator to reject."""
        ...

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        """Prove `desired` against a real Trino that is not the Cluster."""
        ...

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Reasons the Cluster did not adopt this Section, empty if it did.

        Functional, never introspective: ask the Cluster what happened rather than reading
        configuration back (§8). `smoke` is the query Verification just ran, for a Section
        whose adoption is visible in what became of a real query.
        """
        ...


def parses_files(section: Section) -> TypeGuard[ParsesFiles]:
    """Whether this Section can read its files back yet.

    A runtime question for as long as two Sections cannot, and the honest alternative to a
    stub that returns nothing and looks like a Cluster with no catalogs. A TypeGuard, so a
    caller that checks gets the narrower type and one that forgets does not type-check.
    """
    return hasattr(section, "parse_files")
