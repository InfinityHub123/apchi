"""The interactive reference works with no route to the internet.

FastAPI's own `/docs` and `/redoc` load their scripts from a public CDN, and on an air-gapped
network a real deployment saw both answer 200 with a blank page. So the test is the one an
air gap applies: nothing the pages load may come from anywhere but Apchi.
"""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app

#: Every URL a page asks the browser to fetch: script and stylesheet sources, links, and
#: Swagger's own `url:` for the contract.
_FETCHED = re.compile(r"""(?:src|href)=["']([^"']+)["']|url:\s*['"]([^'"]+)['"]""")


@pytest.fixture
async def client(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as http:
        yield http


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
async def test_the_reference_loads_nothing_from_the_internet(
    client: AsyncClient, page: str
) -> None:
    html = (await client.get(page)).text
    fetched = [a or b for a, b in _FETCHED.findall(html)]

    assert fetched, f"{page} fetches nothing, so the pattern above no longer reads it"
    assert [url for url in fetched if "//" in url] == [], f"{page} reaches off the host"
    assert "fonts.googleapis" not in html


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
async def test_everything_the_reference_loads_is_served(client: AsyncClient, page: str) -> None:
    html = (await client.get(page)).text

    for url in [a or b for a, b in _FETCHED.findall(html)]:
        response = await client.get(url)
        assert response.status_code == 200, (
            f"{page} loads {url}, which answers {response.status_code}"
        )
        assert response.content, url


async def test_the_vendored_assets_carry_their_licences() -> None:
    """Swagger UI and Redoc are redistributed in the image, and their licences go with them."""
    from app.api.docs import _STATIC

    assert (_STATIC / "swagger-ui" / "LICENSE").exists()
    assert (_STATIC / "redoc" / "LICENSE").exists()
