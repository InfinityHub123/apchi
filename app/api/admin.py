"""The Admin surface.

Section 19: Admin capabilities sit on a separate surface from the Operator API, and
the stability promise is for the Operator API. Operators never see these paths.

Maintenance Mode is here because Apchi engages it itself after a failed Auto Rollback --
and a state Apchi can engage but nobody can clear would be a trap. The preserved mapping
patterns are here because they are Admin values: applied through the pipeline, never
recorded in a Snapshot (invariant 9, section 14).
"""

import logging

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from app.api.deps import (
    AdminStoreDep,
    ApplyStoreDep,
    MaintenanceStoreDep,
    MutationsEnabled,
)
from app.api.errors import Conflict
from app.pipeline.applies import ApplyRecord, ApplyRunner
from app.pipeline.maintenance import EngagedBy, MaintenanceState
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
    saved = await admin.save(AdminValues(preserved_certificate_mappings=preserved.patterns))
    return PreservedMappings(patterns=saved.preserved_certificate_mappings)


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
