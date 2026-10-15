"""A real OpenID Connect provider, small enough to run inside a test.

Tier 2 drives the flow against Dex (`tests/tier2/test_sso.py`), because a flow proved only
against a fake is a flow nobody has proved. What this is for is the other half: the refusals.
A provider that *will* sign a token with the wrong key, echo the wrong nonce, or leave out the
username claim on request is the only way to assert that Apchi refuses those -- and no real
provider can be asked to misbehave on demand.

So it is a fake in what it decides and real in what it computes: a real RSA key, a real
RS256 signature over a real JWKS, and PKCE verified with its own hashing rather than with
Apchi's, so a bug in `challenge_for` cannot be agreed with into invisibility.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
from httpx import ASGITransport
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

#: Generated once for the whole module. RSA keygen is slow enough that one pair per test
#: would be the suite's largest cost, and what varies between tests is the claims, never the
#: cryptography.
_PUBLISHED = RSAKey.generate_key(2048, parameters={"kid": "first", "alg": "RS256"})
#: Never published. A token signed with it is forged, not merely old.
_FORGED = RSAKey.generate_key(2048, parameters={"kid": "first", "alg": "RS256"})

ISSUER = "http://idp"
CLIENT_ID = "apchi"
CLIENT_SECRET = "the-client-secret-nobody-should-see"
REDIRECT_URL = "http://apchi/api/v1/auth/sso/callback"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class Issued:
    """One authorization code, and what was promised when it was handed out."""

    nonce: str
    challenge: str


@dataclass
class StubProvider:
    """One provider. Every attribute below is a knob a test turns to misbehave."""

    issuer: str = ISSUER
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    #: The claims the ID token carries. Tests replace `groups` to move somebody between roles,
    #: or drop `preferred_username` to prove a login with no readable actor is refused.
    claims: dict[str, Any] = field(
        default_factory=lambda: {
            "preferred_username": "jo",
            "email": "jo@example.test",
            "groups": ["everyone"],
        }
    )
    #: Sign with a key that is *not* in the published JWKS. A forged token, in other words.
    sign_with_an_unpublished_key: bool = False
    #: Echo a nonce other than the one the login asked for: a token minted for a different
    #: login attempt, replayed into this one.
    nonce_to_echo: str | None = None
    #: Answer the token endpoint with no `id_token` at all, as a plain OAuth 2.0 server would.
    omit_id_token: bool = False
    #: Count the exchanges, so a test can prove Apchi made one request and not three.
    exchanges: int = 0
    #: Every ID token this provider has minted, so a test can assert none of them reached the
    #: log. They are bearer credentials for as long as they are valid.
    minted: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._key = _PUBLISHED
        self._issued: dict[str, Issued] = {}
        self.app = Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery),
                Route("/auth", self._authorize),
                Route("/token", self._token, methods=["POST"]),
                Route("/keys", self._keys),
            ]
        )

    # --- the client Apchi talks to ---------------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=ASGITransport(app=self.app), base_url=self.issuer)

    def stop(self) -> None:
        """Take the provider down, answering nothing, as an outage does."""
        self._routes, self.app.router.routes = self.app.router.routes, []

    def start(self) -> None:
        """And bring it back, with the same keys and the same issuer."""
        self.app.router.routes = self._routes

    def rotate(self) -> None:
        """Retire the signing key and publish a new one, as a provider does periodically.

        The JWKS then holds a `kid` Apchi has never seen, which is the normal shape of
        rotation and the thing an ID token verifier has to survive without a restart.
        """
        self._key = RSAKey.generate_key(
            2048, parameters={"kid": secrets.token_hex(4), "alg": "RS256"}
        )

    # --- endpoints -------------------------------------------------------------------------

    async def _discovery(self, request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/auth",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def _keys(self, request: Request) -> Response:
        return JSONResponse(json.loads(json.dumps(KeySet([self._key]).as_dict(private=False))))

    async def _authorize(self, request: Request) -> Response:
        query = request.query_params
        code = secrets.token_urlsafe(16)
        self._issued[code] = Issued(nonce=query["nonce"], challenge=query.get("code_challenge", ""))
        back = urllib.parse.urlencode({"code": code, "state": query["state"]})
        return RedirectResponse(f"{query['redirect_uri']}?{back}", status_code=302)

    async def _token(self, request: Request) -> Response:
        self.exchanges += 1
        form = await request.form()
        code = str(form.get("code", ""))
        issued = self._issued.pop(code, None)
        if issued is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if _challenge(str(form.get("code_verifier", ""))) != issued.challenge:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if self.omit_id_token:
            return JSONResponse({"access_token": "opaque", "token_type": "bearer"})
        return JSONResponse(
            {
                "access_token": "opaque",
                "token_type": "bearer",
                "id_token": self.id_token(self.nonce_to_echo or issued.nonce),
            }
        )

    # --- minting ---------------------------------------------------------------------------

    def id_token(self, nonce: str, **overrides: Any) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "Cg0wLTM4NS0yODA4OS0wEgRtb2Nr",  # opaque, as Dex's really is
            "iat": now,
            "exp": now + 3600,
            "nonce": nonce,
            **self.claims,
            **overrides,
        }
        key = _FORGED if self.sign_with_an_unpublished_key else self._key
        token = jwt.encode({"alg": "RS256", "kid": str(key.kid)}, payload, key)
        self.minted.append(token)
        return token
