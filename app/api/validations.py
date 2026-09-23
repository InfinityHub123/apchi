"""The Validations API: the Validate action of section 9.

Tests a Configuration Candidate without applying it. Bringing up the ephemeral
coordinator takes as long as it takes, so this behaves like an Apply: POST returns
immediately with an identifier, and the verdict is readable by identifier.

Unlike an Apply it does not freeze the Candidate, because it changes nothing.
"""

from fastapi import APIRouter, Request, status

from app.api.deps import CandidateStoreDep, ValidationStoreDep
from app.api.errors import NotFound
from app.pipeline.validations import ValidationRecord, ValidationRunner

router = APIRouter(prefix="/validations", tags=["validations"])


@router.post(
    "",
    response_model=ValidationRecord,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Validate the Configuration Candidate without applying it",
)
async def start_validation(
    request: Request, validations: ValidationStoreDep, candidate_store: CandidateStoreDep
) -> ValidationRecord:
    candidate = await candidate_store.load()
    record = await validations.create(base_snapshot=candidate.base_snapshot)
    runner: ValidationRunner = request.app.state.validation_runner
    runner.start(record)
    return record


@router.get("", response_model=list[ValidationRecord], summary="Past and current Validations")
async def list_validations(validations: ValidationStoreDep) -> list[ValidationRecord]:
    return await validations.list()


@router.get("/{validation_id}", response_model=ValidationRecord, summary="One Validation's verdict")
async def get_validation(validation_id: str, validations: ValidationStoreDep) -> ValidationRecord:
    record = await validations.get(validation_id)
    if record is None:
        raise NotFound(f"No Validation with id {validation_id!r}.")
    return record
