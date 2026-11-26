"""Prometheus' scrape endpoint (#280).

Not under /api/v1: it is not part of the API a client automates against, and /metrics is
where every scraper looks unless told otherwise. No identity, like /health -- a scraper has
none to present -- which is safe only because nothing exported names a resource, an
identity or a credential: counts, stages, kinds, and the paths of mounted certificates.
"""

from fastapi import APIRouter, Request, Response
from pymongo.errors import PyMongoError

from app.metrics import CONTENT_TYPE

router = APIRouter(tags=["health"])


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE: {}}, "description": "The exposition."}},
)
async def metrics(request: Request) -> Response:
    """Answers while MongoDB is down, with the Cluster's state left out of it.

    A scrape that failed with MongoDB would lose the Apply counters and the certificate
    expiries exactly when somebody is looking, and `/health` already reports MongoDB.
    """
    state = request.app.state
    try:
        maintenance = (await state.maintenance_store.get()).engaged
        in_flight = await state.apply_store.in_flight() is not None
    except PyMongoError:
        maintenance = in_flight = None
    body = state.metrics.scrape(maintenance=maintenance, in_flight=in_flight)
    return Response(content=body, media_type=CONTENT_TYPE)
