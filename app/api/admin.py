"""The Admin surface.

Section 19: Admin capabilities sit on a separate surface from the Operator API, and
the stability promise is for the Operator API. Operators never see these paths.

Maintenance Mode is the one Admin capability slice 1 needs, because Apchi engages it
itself after a failed Auto Rollback -- and a state Apchi can engage but nobody can
clear would be a trap.
"""

from fastapi import APIRouter

from app.api.deps import MaintenanceStoreDep
from app.pipeline.maintenance import EngagedBy, MaintenanceState

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
