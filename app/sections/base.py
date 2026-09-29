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
from typing import Any, Protocol

from pydantic import BaseModel

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.sections import SectionName

#: One Section's resources as they are stored in the Candidate and in Snapshots, keyed
#: by resource name. Deliberately untyped here: the shape belongs to the Section's own
#: model, and the pipeline never looks inside it.
Resources = dict[str, Any]


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
class CoordinatorFile:
    """A file Apchi delivers to the coordinator and owns the mount for.

    Owning the mount is not a detail. Trino refuses to start when a file it was told to
    read is missing, and Kubernetes turns a subPath mount of an absent Secret key into a
    *directory* it dies on -- so "this Section is empty" can only be expressed by the mount
    not being there, which means Apchi adds and removes it on the Admin's pod template.
    Everything else on that template belongs to the Admin, and a precondition rejects
    anything of theirs mounted at a path declared here.
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


@dataclass(frozen=True)
class Cluster:
    """Everything a Section is allowed to touch.

    Passed as one object so that adding a capability a Section needs does not change
    every Section's signature.
    """

    trino: Trino
    kubernetes: KubernetesAdapter
    settings: Settings


class SectionPlan(Protocol):
    """What a Section would do to the Cluster. Computed before anything is issued, so
    it can be reported and logged before the Cluster changes."""

    @property
    def empty(self) -> bool: ...

    def summary(self) -> str: ...


class Section(Protocol):
    """One managed area of a Configuration Candidate."""

    name: SectionName

    #: Whether Trino adopts this Section only by restarting. A property of how Trino
    #: consumes the configuration, never of when an Operator's edit takes effect.
    requires_rollout: bool

    def coordinator_file(self, settings: Settings) -> CoordinatorFile | None:
        """The file this Section delivers to the coordinator, or None if it delivers none.

        Declaring it is all a Section does about delivery: the pipeline writes the Secret,
        mounts it when there is content, unmounts it when there is not, puts it in front of
        the validation probe, and tells the preconditions to guard the path.
        """
        ...

    def render_file(self, desired: Resources, settings: Settings) -> str | None:
        """The file's content, or None when this Section has nothing to deliver.

        None and empty mean the same thing to the pipeline: no file, so no mount.
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

    async def verify(self, cluster: Cluster, desired: Resources) -> list[str]:
        """Reasons the Cluster did not adopt this Section, empty if it did."""
        ...
