"""Serving the single-page UI from the same origin as the API.

Not a convenience. Apchi's session is an `httponly`, `samesite=lax` cookie (`app/api/auth.py`),
and a `lax` cookie is not sent on a cross-origin request -- so a UI served from its own origin
could not authenticate at all, whatever CORS allowed. Keeping both on one origin is what makes
the cookie work, and it is also what makes §22's "no privileged backdoor" true by
construction: the UI reaches the same `/api/v1` every other caller does.

The mount is conditional on the build being present. A development Apchi runs with the Vite
dev server in front of it and `web/dist` empty, and an Apchi whose image was built without the
UI should serve its API rather than refuse to start.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

#: Where the image puts the built UI. Overridden in tests, never configurable at runtime: a
#: path a deployment could point anywhere is a way to serve something Apchi did not build.
DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


#: The two routes mounting the UI adds, named so tests/tier1/test_identity.py can assert the
#: served surface instead of discovering it. Both answer with the entry document and nothing
#: else; every byte of data the UI shows comes from a gated `/api/v1` route.
UI_ROUTES = {"/", "/{path:path}"}


def mount_web(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built UI at `/`, or leave the app API-only. True when it was mounted.

    `dist` is read at call time rather than bound as a default, so a test can point it
    somewhere empty and get the API-only app deterministically -- which matters because
    whether this mounts otherwise depends on whether somebody has run a frontend build.
    """
    dist = DIST if dist is None else dist
    index = dist / "index.html"
    if not index.is_file():
        logger.info("no UI build at %s; serving the API only", dist)
        return False

    # The hashed assets are immutable and the entry document must never be cached, or a
    # deployment serves a new index against an old bundle and the UI breaks with no error.
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index_document() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    # A client-side route reached by typing or reloading has to answer with the document, not
    # a 404. Registered last so every real route above it still wins, and scoped to GET so a
    # mistyped POST still fails loudly instead of silently returning HTML.
    @app.get("/{path:path}", include_in_schema=False)
    async def spa_route(request: Request, path: str) -> Response:
        if path.startswith("api/"):
            return Response(status_code=404)
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    logger.info("serving the UI from %s", dist)
    return True
