"""The interactive API reference, served entirely from the image.

FastAPI's own `/docs` and `/redoc` load Swagger UI and Redoc from a public CDN, and Redoc its
fonts from Google. On a network with no route to the internet -- which is where a real
deployment ran -- both answered 200 with a blank page while `/openapi.json` worked. So the
assets are vendored under `app/static` at pinned versions, with their licences, and the two
pages are rebuilt here to point at them.

Vendored rather than taken from a package that bundles them: it is two files each, and a
dependency for a presentational concern is one more thing in the supply chain.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

_STATIC = Path(__file__).resolve().parent.parent / "static"

#: The versions in `app/static`, which is what to bump them by.
SWAGGER_UI_VERSION = "5.34.1"
REDOC_VERSION = "2.5.4"


def install_docs(app: FastAPI) -> None:
    """`/docs`, `/docs/oauth2-redirect` and `/redoc`, as FastAPI serves them, offline.

    The FastAPI app must be built with `docs_url=None, redoc_url=None`, or its CDN-backed
    pages would answer first.
    """
    app.mount("/docs/assets", StaticFiles(directory=_STATIC), name="docs-assets")
    title = f"{app.title} API"
    openapi_url = app.openapi_url or "/openapi.json"

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=title,
            swagger_js_url="/docs/assets/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/docs/assets/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/docs/assets/swagger-ui/favicon-32x32.png",
            oauth2_redirect_url="/docs/oauth2-redirect",
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    async def redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=openapi_url,
            title=title,
            redoc_js_url="/docs/assets/redoc/redoc.standalone.js",
            redoc_favicon_url="/docs/assets/swagger-ui/favicon-32x32.png",
            # Google Fonts is a third request to the internet, and the bundle's own font
            # stack renders the page without it.
            with_google_fonts=False,
        )
