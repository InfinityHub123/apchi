"""Health, which is about Apchi's own liveness -- not the Cluster's.

Verification (section 8) is what decides whether the Trino Cluster is healthy;
this endpoint only reports whether Apchi can serve requests and reach its store.
"""

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    mongo: str


@router.get("/health", response_model=Health, summary="Apchi's own liveness")
async def health(request: Request, response: Response) -> Health:
    reachable = await request.app.state.mongo.ping()
    if not reachable:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Health(status="ok" if reachable else "degraded", mongo="up" if reachable else "down")
