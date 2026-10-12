"""The Admin surface.

Section 19: Admin capabilities sit on a separate surface from the Operator API, and
the stability promise is for the Operator API. Operators never see these paths.

Maintenance Mode is here because Apchi engages it itself after a failed Auto Rollback --
and a state Apchi can engage but nobody can clear would be a trap. The preserved mapping
patterns are here because they are Admin values: applied through the pipeline, never
recorded in a Snapshot (invariant 9, section 14).
"""

import logging
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    AdminStoreDep,
    ApplyStoreDep,
    CandidateStoreDep,
    MaintenanceStoreDep,
    MutationsEnabled,
)
from app.api.errors import Conflict
from app.pipeline.applies import ApplyRecord, ApplyRunner
from app.pipeline.configuration import (
    AdminImport,
    Configuration,
    accepted,
    of_cluster,
)
from app.pipeline.maintenance import EngagedBy, MaintenanceState
from app.sections import SectionName
from app.sections.admin import MAX_PRESERVED_MAPPINGS, AdminValues
from app.sections.certificate_mapping.model import CertificateMappingWrite
from app.sections.certificate_mapping.section import validate_patterns

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

DEFAULT_REASON = "An Admin has engaged Maintenance Mode."


@router.get(
    "/maintenance-mode",
    response_model=MaintenanceState,
    summary="Whether Operator changes are currently disabled",
)
async def get_maintenance_mode(maintenance: MaintenanceStoreDep) -> MaintenanceState:
    return await maintenance.get()


@router.put(
    "/maintenance-mode",
    response_model=MaintenanceState,
    summary="Disable or re-enable Operator changes",
)
async def set_maintenance_mode(
    state: MaintenanceState, maintenance: MaintenanceStoreDep
) -> MaintenanceState:
    """Releasing is how an incident is closed: an Admin has looked at the Cluster and
    is satisfied it is in a state Operators can safely edit again."""
    if not state.engaged:
        return await maintenance.release()
    return await maintenance.engage(state.reason or DEFAULT_REASON, EngagedBy.ADMIN)


class PreservedMappings(BaseModel):
    """The patterns a Cluster ran before Apchi, kept alive while its clients migrate."""

    patterns: list[CertificateMappingWrite] = Field(
        default_factory=list,
        max_length=MAX_PRESERVED_MAPPINGS,
        description=(
            "Evaluated in the order given, beneath the Operator's pattern, so a subject "
            "matching both resolves to the convention being migrated to."
        ),
    )


@router.get(
    "/certificate-mapping/preserved",
    response_model=PreservedMappings,
    summary="Mapping patterns preserved through a migration",
)
async def get_preserved_mappings(admin: AdminStoreDep) -> PreservedMappings:
    return PreservedMappings(patterns=(await admin.load()).preserved_certificate_mappings)


@router.put(
    "/certificate-mapping/preserved",
    response_model=PreservedMappings,
    summary="Replace the preserved mapping patterns",
    dependencies=[MutationsEnabled],
)
async def set_preserved_mappings(
    preserved: PreservedMappings, admin: AdminStoreDep
) -> PreservedMappings:
    """Stages nothing and reaches nothing. The Cluster gets this at the next Apply --
    an Admin Apply if the Admin wants it now, an Operator Apply otherwise.

    Apchi does not time the migration. It schedules no removal, counts nothing down and
    never warns that a preserved pattern is overdue: only the Admin knows whether their
    clients have moved, and a deadline Apchi invented would be a deadline it could enforce
    by accident.
    """
    validate_patterns(preserved.patterns)
    # Loaded and copied rather than built fresh: `save` replaces the whole document, so an
    # `AdminValues(...)` built from this request alone would silently reset every other
    # Admin value to its default -- including turning permission enforcement back off.
    values = await admin.load()
    saved = await admin.save(
        values.model_copy(update={"preserved_certificate_mappings": preserved.patterns})
    )
    return PreservedMappings(patterns=saved.preserved_certificate_mappings)


class PermissionEnforcement(BaseModel):
    """Whether a Trino Identity may reach only what it has been granted."""

    model_config = ConfigDict(extra="forbid")

    enforced: bool = Field(
        description=(
            "False leaves the generated rules ending in a catch-all that allows everything "
            "to everyone, which is what a Cluster did before Apchi was installed."
        )
    )


@router.get(
    "/permissions/enforcement",
    response_model=PermissionEnforcement,
    summary="Whether grants are enforced",
)
async def get_enforcement(admin: AdminStoreDep) -> PermissionEnforcement:
    return PermissionEnforcement(enforced=(await admin.load()).enforce_permissions)


@router.put(
    "/permissions/enforcement",
    response_model=PermissionEnforcement,
    summary="Decide whether grants are enforced",
    dependencies=[MutationsEnabled],
)
async def set_enforcement(
    enforcement: PermissionEnforcement, admin: AdminStoreDep
) -> PermissionEnforcement:
    """An Admin's decision, and an Admin's timing.

    Turning this on is what makes a grant mean something, and on a Cluster already serving
    users it denies every identity without a grant everything it had. An Operator cannot do
    it -- they would be closing a Cluster by editing configuration -- and Apchi will not do
    it on their behalf. It stages nothing by itself: the Cluster gets it at the next Apply,
    which Review reports as costing no restart because Trino re-reads the rules on a timer.
    """
    values = await admin.load()
    saved = await admin.save(
        values.model_copy(update={"enforce_permissions": enforcement.enforced})
    )
    return PermissionEnforcement(enforced=saved.enforce_permissions)


@router.post(
    "/applies",
    response_model=ApplyRecord,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Apply an Admin change without creating a Snapshot",
    dependencies=[MutationsEnabled],
)
async def start_admin_apply(request: Request, applies: ApplyStoreDep) -> ApplyRecord:
    """The same pipeline, over the latest Snapshot merged with today's Admin values.

    Not the Candidate: an Admin Apply must not push an Operator's staged, unreviewed
    changes to the Cluster. And no Snapshot at the end, because nothing an Operator owns
    changed -- what this Apply did is recorded as an Apply and nowhere else (section 14).

    It freezes the Candidate exactly as an Operator Apply does. Both mutate one Cluster,
    and the pipeline's guarantees depend on nothing else changing underneath.
    """
    if (running := await applies.in_flight()) is not None:
        raise Conflict(
            f"Apply {running.id} is already in flight. The Candidate is frozen until it finishes."
        )
    latest = await request.app.state.snapshot_store.latest()
    record = await applies.create(base_snapshot=None if latest is None else latest.number)
    runner: ApplyRunner = request.app.state.admin_apply_runner
    runner.start(record)
    logger.info("admin apply started", extra={"apply": record.id})
    return record


class AdminConfiguration(BaseModel):
    """What an Admin import left behind, both halves of it."""

    base_snapshot: int | None = Field(
        description=(
            "This Cluster's latest Snapshot, which is what Review now diffs against -- "
            "never the Snapshot named in the imported document's provenance."
        )
    )
    sections: dict[SectionName, dict[str, Any]]
    admin: AdminValues = Field(
        description="Every Admin value as it now stands, including the ones left unmentioned."
    )


@router.get(
    "/configuration/export",
    response_model=Configuration,
    summary="The full configuration: the Sections and the Admin values",
)
async def export_configuration(
    candidates: CandidateStoreDep, admin: AdminStoreDep
) -> Configuration:
    """What makes this the *full* configuration is the Admin values.

    Admin values are applied through the pipeline and never recorded in a Snapshot
    (invariant 9, §14), so a Snapshot export is less than what the Cluster is running. This
    is the pair. It is the only difference between the two scopes, and it explains itself:
    an Operator cannot set them, so an Operator's export must not carry them.

    One configuration, not every Snapshot. History lives in MongoDB, whose durability is
    the platform's concern rather than Apchi's (#110).

    **The response holds real credentials**, for the same reason a Snapshot export does.
    """
    candidate = await candidates.load()
    return of_cluster(candidate.base_snapshot, candidate.sections, await admin.load())


@router.put(
    "/configuration",
    response_model=AdminConfiguration,
    summary="Replace the Sections and the Admin values together",
    dependencies=[MutationsEnabled],
)
async def import_configuration(
    document: AdminImport,
    candidates: CandidateStoreDep,
    admin: AdminStoreDep,
) -> AdminConfiguration:
    """The Admin scope's import: the six Sections, and the Admin values with them.

    **It replaces every Admin value**, including the ones the document does not mention,
    because that is what replacing means. A document with `enforce_permissions` absent
    turns enforcement off across the Cluster, and one with no preserved patterns drops the
    rules an adopted Cluster was relying on. That is why `admin` is required rather than
    defaulted -- an omission that large should not be expressible -- and why the response
    says what the Admin values now are instead of leaving a caller to assume only what they
    sent changed.

    Nothing reaches the Cluster. The next Apply is what delivers it, as with every other
    Admin value (`PUT /admin/permissions/enforcement` and the preserved patterns are the
    same shape).
    """
    # Both halves validated before either is written, so a refusal stores nothing. The
    # Sections' problems are reported whole; an unusable preserved pattern is refused the
    # way `PUT /admin/certificate-mapping/preserved` refuses one, because it is the same
    # value reaching the same file.
    sections = accepted(document)
    validate_patterns(document.admin.preserved_certificate_mappings)

    # Sections first, deliberately. These are two documents and there is no transaction
    # across them, so one of them can land alone. Sections landing alone leaves an imported
    # configuration staged against the Admin values already in force -- stale, and fixed by
    # sending the document again. Admin values landing alone could leave permission
    # enforcement switched off against a Candidate nobody imported, which is the half not
    # to be left holding.
    candidate = await candidates.replace(sections)
    values = await admin.save(document.admin)
    logger.info(
        "configuration imported",
        extra={"base_snapshot": candidate.base_snapshot, "admin": True},
    )
    return AdminConfiguration(
        base_snapshot=candidate.base_snapshot, sections=candidate.sections, admin=values
    )
