"""Health, which is about Apchi's own liveness -- not the Cluster's.

Verification (section 8) is what decides whether the Trino Cluster is healthy;
this endpoint only reports whether Apchi can serve requests and reach its store.
"""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from app.pipeline.history import History

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    mongo: str
    history: Literal["intact", "lost"] = Field(
        description=(
            "Whether MongoDB still holds every Snapshot this Cluster committed. `lost` "
            "means it does not, and the Cluster is running a configuration Apchi can no "
            "longer reproduce from its own records (§18)."
        )
    )
    snapshots_held: int | None = Field(
        default=None, description="The highest Snapshot MongoDB holds."
    )
    snapshots_ever: int | None = Field(
        default=None,
        description="The highest Snapshot ever committed here, read from outside MongoDB.",
    )


@router.get("/health", response_model=Health, summary="Apchi's own liveness")
async def health(request: Request, response: Response) -> Health:
    """Apchi's own liveness, and whether its memory is all there.

    Lost history does **not** make this 503, and that is deliberate rather than lenient.
    This endpoint is the chart's readiness *and* liveness probe, so a 503 here is how
    Kubernetes is told to stop sending traffic and then to kill the pod -- which is right
    while MongoDB is unreachable and exactly wrong after a loss, because restoring a backup
    means talking to an Apchi that is up. A pod crashlooping on the one condition an
    Operator has to log in and fix would make §18 unrecoverable through Apchi's own API.
    """
    reachable = await request.app.state.mongo.ping()
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    recorded: History = request.app.state.history
    return Health(
        status="ok" if reachable else "degraded",
        mongo="up" if reachable else "down",
        history="intact" if recorded.intact else "lost",
        snapshots_held=recorded.held,
        snapshots_ever=recorded.ever,
    )
