"""The Certificate Mapping Section: the operations the pipeline and the API share.

Rollout-required. `UserMapping` parses an immutable rule list when the authenticator is
constructed, so a change is adopted only by a new pod. See section 13.3.
"""

import logging
import re
from collections.abc import Sequence

from app.adapters.trino import Trino
from app.api.errors import NotFound, UnprocessablePayload
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
from app.sections.certificate_mapping import RESOURCE, SECTION
from app.sections.certificate_mapping.generator import MOUNT_PATH, render_rules
from app.sections.certificate_mapping.model import CertificateMapping, CertificateMappingWrite

logger = logging.getLogger(__name__)

#: Which authenticator reads the file is the Admin's choice -- certificate, password or
#: insecure -- and they all parse it with the same UserMapping. The probe uses the insecure
#: one because it is the only one that needs no TLS, and what is being validated is the file.
PROBE_CONFIG = {"http-server.authentication.insecure.user-mapping.file": MOUNT_PATH}

_GROUP_REFERENCE = re.compile(r"\$(\d+)")


def get_mapping(stored: Resources) -> CertificateMapping:
    if RESOURCE not in stored:
        raise NotFound("No Certificate Mapping Pattern is configured.")
    return CertificateMapping.model_validate(stored[RESOURCE])


def set_mapping(stored: Resources, write: CertificateMappingWrite) -> CertificateMapping:
    _validated(write)
    stored[RESOURCE] = write.model_dump(mode="json")
    return CertificateMapping.model_validate(stored[RESOURCE])


def clear_mapping(stored: Resources) -> None:
    if RESOURCE not in stored:
        raise NotFound("No Certificate Mapping Pattern is configured.")
    del stored[RESOURCE]


def _problems_with(write: CertificateMappingWrite, prefix: str = "") -> list[dict[str, str]]:
    """Why a pattern could not produce an identity, if it could not.

    Trino uses Java's regex engine, so this is an approximation and the ephemeral
    coordinator remains the authority. What it catches is the mistake worth catching at
    request time: a replacement referring to a group the pattern does not have, which
    produces no identity at all rather than a wrong one.
    """
    problems: list[dict[str, str]] = []
    try:
        compiled = re.compile(write.pattern)
    except re.error as exc:
        problems.append(
            {"property": f"{prefix}pattern", "problem": f"not a valid expression: {exc}"}
        )
    else:
        wanted = [int(n) for n in _GROUP_REFERENCE.findall(write.user)]
        missing = [n for n in wanted if n > compiled.groups]
        if missing:
            problems.append(
                {
                    "property": f"{prefix}user",
                    "problem": (
                        f"refers to capturing group {missing[0]}, but the pattern has "
                        f"{compiled.groups}. Nothing would be substituted, so the pattern "
                        "would produce no identity."
                    ),
                }
            )
    return problems


def _validated(write: CertificateMappingWrite) -> None:
    if problems := _problems_with(write):
        raise UnprocessablePayload(
            "The Certificate Mapping Pattern is not usable.", details=problems
        )


def validate_patterns(patterns: Sequence[CertificateMappingWrite]) -> None:
    """The same checks over a list of preserved patterns, naming which one.

    An Admin pasting a Cluster's existing rules in gets the index back, because with
    several of them "the pattern is invalid" does not say which pattern.
    """
    problems = [
        problem
        for index, pattern in enumerate(patterns)
        for problem in _problems_with(pattern, prefix=f"patterns[{index}].")
    ]
    if problems:
        raise UnprocessablePayload("A preserved mapping pattern is not usable.", details=problems)


class CertificateMappingSection:
    """The Certificate Mapping Section as the pipeline sees it."""

    name: SectionName = SECTION
    #: UserMapping's rule list is immutable once the authenticator exists.
    requires_rollout = True

    def coordinator_file(self, settings: Settings) -> CoordinatorFile:
        return CoordinatorFile(
            secret=settings.certificate_mapping_secret_name,
            volume=settings.certificate_mapping_volume_name,
            path=MOUNT_PATH,
            probe_config=PROBE_CONFIG,
        )

    def render_file(self, desired: Resources, settings: Settings, admin: AdminValues) -> str:
        """Always a file, never nothing.

        An absent file is not the same as no pattern: the authenticator is configured to
        read one, and Trino refuses to start when it is missing. "No pattern" is therefore a
        file whose rule leaves every name as presented, which is exactly Trino's behaviour
        with no mapping configured.

        The reserved rule needs the identity Apchi authenticates as, which is a deployment
        setting -- so it is read here rather than captured when the registry is built.
        """
        return render_rules(desired, settings.trino_user, admin.preserved_certificate_mappings)

    def plan(self, desired: Resources, current: Resources) -> "MappingPlan":
        return MappingPlan(changed=desired.get(RESOURCE) != current.get(RESOURCE))

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
        """Only when an Operator configured something. The file Apchi writes for an empty
        Section is the one Trino behaves as though it had anyway."""
        return bool(desired)

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        """Starting is the check: a rule list Trino cannot parse is a pod that will not
        start, which the pipeline turns into a failure naming this Section."""
        return []

    async def verify(self, cluster: Cluster, desired: Resources) -> list[str]:
        """Nothing to assert.

        Proving the mapping works would mean presenting a certificate, and Apchi connects
        as its own identity. What Verification does prove is that the Cluster came back --
        and because Apchi's own rule is in the file, coming back means Apchi can still
        reach it, which is the failure that would otherwise be silent.
        """
        return []


class MappingPlan:
    """Changed or not. A singleton has no richer diff to report."""

    def __init__(self, changed: bool) -> None:
        self.changed = changed

    @property
    def empty(self) -> bool:
        return not self.changed

    def summary(self) -> str:
        return "changed" if self.changed else "no changes"
