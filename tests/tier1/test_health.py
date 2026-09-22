"""Apchi's own liveness, through the HTTP API."""

from httpx import AsyncClient


async def test_health_reports_ok_when_mongo_is_reachable(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mongo": "up"}


async def test_health_is_degraded_when_mongo_is_unreachable(client: AsyncClient) -> None:
    from app.config import Settings
    from app.main import create_app

    unreachable = Settings(mongo_uri="mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200")
    app = create_app(unreachable)
    async with (
        AsyncClient(
            transport=__import__("httpx").ASGITransport(app=app), base_url="http://apchi"
        ) as offline,
        app.router.lifespan_context(app),
    ):
        response = await offline.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "mongo": "down"}


async def test_openapi_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]


async def test_apchi_starts_when_mongo_is_unreachable() -> None:
    """Starting degraded beats crashlooping: the health endpoint is the readiness
    signal, and a pod that dies on a transient blip loses its logs."""
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(mongo_uri="mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200"))

    async with app.router.lifespan_context(app):
        pass  # startup recovery cannot run, and that must not stop the app
