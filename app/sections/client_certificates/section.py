"""The Client Certificates Section: the operations the pipeline and the API share.

No Rollout. A certificate is read when a connection is opened, and the directory it lands in
is already mounted -- so uploading one costs no queries (§7.2, ADR-0005). What it costs
instead is that the file is not there instantly: the kubelet has to project the Secret, which
is why the Catalog DDL that references a certificate is retried rather than issued once.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.adapters.trino import Trino
from app.api.errors import NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorDirectory,
    Delivery,
    DiscoveredPaths,
    Parsed,
    Resources,
    SectionPlan,
    SmokeQuery,
    Unaccounted,
    ValidationFailure,
)
from app.sections.client_certificates import SECTION
from app.sections.client_certificates.bundle import Bundle
from app.sections.client_certificates.generator import MOUNT_DIR, parse, render, rendered_size
from app.sections.client_certificates.model import ClientCertificate, describe

logger = logging.getLogger(__name__)


def list_certificates(
    stored: Resources, expiring_within_days: int, status: str | None = None
) -> list[ClientCertificate]:
    """Every staged certificate, newest expiry last, optionally filtered by status.

    The filter is the point of the status at all: an Operator wants to find what is expiring
    before a Catalog stops connecting, not to read expiry dates one at a time.
    """
    described = [
        describe(name, stored[name]["certificate"], expiring_within_days) for name in sorted(stored)
    ]
    return [c for c in described if status is None or c.status == status]


def get_certificate(stored: Resources, name: str, expiring_within_days: int) -> ClientCertificate:
    if name not in stored:
        raise NotFound(f"No client certificate named {name!r} in the Configuration Candidate.")
    return describe(name, stored[name]["certificate"], expiring_within_days)


def store_certificate(
    stored: Resources, name: str, bundle: Bundle, settings: Settings
) -> ClientCertificate:
    """Upload, or replace under the same name.

    Replacing is how renewal works, and it has to keep the name: the Catalogs referencing it
    reference the name, so a renewal that had to be uploaded under a new one would mean
    editing every Catalog that uses it.
    """
    candidate = dict(stored)
    candidate[name] = {
        "certificate": bundle.certificate_pem,
        "private_key": bundle.private_key_pem,
    }
    if (size := rendered_size(candidate)) > settings.client_certificate_max_bytes:
        raise UnprocessablePayload(
            f"Storing {name!r} would take the client certificate Secret to {size} bytes, "
            f"past the {settings.client_certificate_max_bytes} a Kubernetes Secret may "
            "hold. Remove a certificate "
            "that is no longer used, or split this Cluster's certificates across Clusters.",
            details=[{"property": "archive", "problem": "the Secret would be too large"}],
        )
    stored[name] = candidate[name]
    return describe(name, bundle.certificate_pem, settings.certificate_expiring_within_days)


def delete_certificate(stored: Resources, name: str) -> None:
    if name not in stored:
        raise NotFound(f"No client certificate named {name!r} in the Configuration Candidate.")
    del stored[name]


@dataclass
class CertificatesPlan:
    """Which certificates moved."""

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


class ClientCertificatesSection:
    """The Client Certificates Section as the pipeline sees it."""

    name: SectionName = SECTION
    #: A certificate is read when a connection is opened, from a directory already mounted.
    requires_rollout = False

    def coordinator_files(self, settings: Settings) -> tuple[Delivery, ...]:
        """One directory, mounted by the Admin, holding two files per certificate.

        A directory rather than a list of files because how many there are is the
        Candidate's business: Apchi cannot declare in advance what an Operator will upload.
        """
        return (
            CoordinatorDirectory(secret=settings.client_certificate_secret_name, path=MOUNT_DIR),
        )

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        return render(desired)

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """A directory, and the one Section nothing in Trino's configuration points at.

        Certificates are referenced from a connector's own properties as a path, so the only
        statement of where they live is in the catalogs -- and those are #88's problem. Apchi
        looks where Apchi would put them and says that is what it did, because on a Cluster
        Apchi did not configure they could be anywhere, and reporting an empty directory as
        "no certificates" would be a guess dressed as a fact.
        """
        return DiscoveredPaths(
            directories={MOUNT_DIR: MOUNT_DIR},
            why=(
                "nothing in Trino's configuration names a certificate directory, so this is "
                "where Apchi would mount one rather than where the Cluster says it is"
            ),
        )

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """An empty directory means no certificates, which mounts as an empty directory
        anyway -- so there is nothing to distinguish and nothing to refuse.

        Metadata is not recovered here because it is not stored: `describe` derives the CN,
        subject, issuer and expiry from the certificate on every read, so a parsed
        certificate reports the same things an uploaded one does, from the same bytes. A
        parse that carried metadata of its own would be a second truth about an expiry.
        """
        resources, unaccounted = parse(files)
        return Parsed(
            resources=resources,
            unaccounted=tuple(
                Unaccounted(path=path, what=what, content=content)
                for path, what, content in unaccounted
            ),
        )

    def plan(self, desired: Resources, current: Resources) -> CertificatesPlan:
        return CertificatesPlan(
            added=[name for name in sorted(desired) if name not in current],
            changed=[
                name
                for name in sorted(desired)
                if name in current and desired[name] != current[name]
            ],
            removed=sorted(set(current) - set(desired)),
        )

    async def apply(self, cluster: Cluster, desired: Resources, plan: SectionPlan) -> None:
        """Nothing beyond the files, which the pipeline has delivered."""

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewriting the directory is the whole undo, and the pipeline has done it."""
        return False

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        """Nothing to check across the Candidate.

        Everything about a certificate was judged when the archive was read: a bundle that is
        not a pair never reached the Candidate. Whether a Catalog references one that exists
        is the Catalogs side of the question, and belongs with the Catalog.
        """
        return []

    def needs_probe(self, desired: Resources) -> bool:
        """No. Trino does not read these at startup -- a certificate is read when a
        connection is opened -- so a probe would prove nothing a parse has not."""
        return False

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        return []

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """Nothing Apchi can assert.

        Whether a certificate works is whether the data source accepts it, which only a
        Catalog using it can show -- and that is the Catalog's Verification, not this one's.
        Apchi cannot see the pod's filesystem to check the file even arrived.
        """
        return []
