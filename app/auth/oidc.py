"""Apchi as an OpenID Connect *client*, and never an identity provider.

The flow is authorization code with PKCE: Apchi sends the browser to the provider, the
provider sends it back with a code, and Apchi exchanges that code -- over its own connection,
with its client secret -- for an ID token it then verifies. Because Apchi only ever plays the
client, this works against Keycloak, Authentik, Zitadel, Okta or Entra without Apchi knowing
which of them it is talking to.

**Everything is discovered from the issuer**, not configured endpoint by endpoint. A provider
that rotates its signing keys or moves an endpoint keeps working, and a mistyped endpoint
fails at configuration time rather than during the one login somebody needed.

**Nothing here degrades.** Discovery that fails, a signature that does not verify, a claim
that is missing: all of them raise, and login fails. A control plane that quietly dropped its
own authentication because an identity provider was unreachable would be worse than one that
refuses to let anybody in -- it would look like it was working.

### Why joserfc rather than Authlib

#132 called for Authlib, on the grounds that JWKS signature validation is the piece nobody
should write twice. That part is right, and it is `joserfc` -- the Authlib project's own JOSE
library, which is what Authlib itself now depends on. What Authlib adds on top is a framework
integration Apchi cannot use as it stands:

* Its Starlette client keeps `state`, `nonce` and the PKCE verifier in `request.session`, so
  it requires Starlette's `SessionMiddleware`: a signed client-side cookie and a signing key
  shared across replicas. Apchi keeps that data in MongoDB instead (`logins.py`), which also
  makes a replayed `state` refusable by construction.
* `authlib.integrations.httpx_client`, which the Starlette client imports, is deprecated in
  Authlib 1.8 in favour of a separate `httpx2` distribution. Apchi already uses `httpx` for
  Trino and would be carrying two HTTP libraries to borrow one redirect helper.

So: `joserfc` for the cryptography, `httpx` for three documented HTTP calls.
"""

import base64
import hashlib
import hmac
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization

from app.auth.identity import Role
from app.auth.logins import PendingLogin
from app.config import Settings

logger = logging.getLogger(__name__)

#: Signature algorithms Apchi will accept on an ID token, whatever the provider advertises.
#: Asymmetric only, and the omissions are the point: `none` is an unsigned token, and the
#: HMAC family would verify an attacker's token against a shared secret rather than against
#: the provider's published key. The intersection with discovery is taken, so a provider that
#: offers only RS256 is held to RS256.
SIGNING_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"})


class OidcError(Exception):
    """Anything that stops a login completing. Carries a reason for the log, never for the
    caller: what the browser is told is that the login failed."""


class OidcNotConfigured(OidcError):
    """`sso` mode without the settings it needs. Raised on every request rather than at
    startup, because a pod that refuses to start takes `/health` down with it and §19's
    answer to a broken configuration is a Cluster that says so, not one that is gone."""


@dataclass(frozen=True)
class Discovered:
    """The provider's own description of itself, as `openid-configuration` returns it."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: frozenset[str]
    #: True when the provider advertises `client_secret_basic`. OIDC makes that the default
    #: for a confidential client, and some providers reject the secret in the body when the
    #: client is registered for the header, so the advertised method is the one Apchi uses.
    secret_in_header: bool


def challenge_for(code_verifier: str) -> str:
    """The S256 PKCE challenge: base64url of the SHA-256 of the verifier, unpadded (RFC 7636
    §4.2). `plain` is never offered -- it puts the verifier in the URL the challenge exists to
    keep it out of."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OidcProvider:
    """One identity provider, discovered once and cached.

    The HTTP client is injected so tests can serve a provider in process. Nothing else about
    this class is substitutable: the signature check, the claim checks and the refusal to
    degrade are the behaviour under test, not scaffolding around it.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._owns_http = http is None
        self._discovered: Discovered | None = None
        self._keys: KeySet | None = None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # --- configuration -------------------------------------------------------------------

    @property
    def issuer(self) -> str:
        return self._required("oidc_issuer")

    @property
    def client_id(self) -> str:
        return self._required("oidc_client_id")

    @property
    def redirect_url(self) -> str:
        return self._required("oidc_redirect_url")

    def _required(self, name: str) -> str:
        value = getattr(self._settings, name)
        if not value:
            raise OidcNotConfigured(
                f"APCHI_AUTH_MODE is 'sso' but APCHI_{name.upper()} is not set."
            )
        return str(value)

    # --- discovery -----------------------------------------------------------------------

    async def discovered(self) -> Discovered:
        """Fetched once per process, then cached.

        Not cached on failure: a provider that was down while Apchi started has to be usable
        once it comes back, without restarting the pod.
        """
        if self._discovered is None:
            self._discovered = await self._discover()
        return self._discovered

    async def _discover(self) -> Discovered:
        # The path is fixed by OIDC Discovery §4 and is appended to the issuer, not joined
        # with it: an issuer with a path (`http://host/dex`) keeps that path.
        url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        document = await self._json("GET", url, what="discovery")
        try:
            issuer = str(document["issuer"])
            endpoints = (
                str(document["authorization_endpoint"]),
                str(document["token_endpoint"]),
                str(document["jwks_uri"]),
            )
        except KeyError as missing:
            raise OidcError(f"{url} is missing {missing}") from missing

        # The issuer in the document must be the issuer Apchi was configured with, and is
        # also what every ID token's `iss` is then checked against. A provider served under a
        # name that does not match its own `issuer` would otherwise authenticate against one
        # identity and be verified against another.
        if issuer != self.issuer:
            raise OidcError(
                f"{url} describes issuer {issuer!r}, but APCHI_OIDC_ISSUER is {self.issuer!r}"
            )

        advertised = {str(a) for a in document.get("id_token_signing_alg_values_supported", [])}
        algorithms = frozenset(advertised & SIGNING_ALGORITHMS) or frozenset({"RS256"})
        methods = {str(m) for m in document.get("token_endpoint_auth_methods_supported", [])}
        logger.info(
            "discovered the identity provider",
            extra={"issuer": issuer, "algorithms": sorted(algorithms)},
        )
        return Discovered(
            issuer=issuer,
            authorization_endpoint=endpoints[0],
            token_endpoint=endpoints[1],
            jwks_uri=endpoints[2],
            algorithms=algorithms,
            # Absent means the provider said nothing, and OIDC's default for a confidential
            # client is the header.
            secret_in_header="client_secret_basic" in methods or not methods,
        )

    async def _json(self, method: str, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, url, **kwargs)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as failure:
            raise OidcError(f"{what} failed: {failure}") from failure
        except ValueError as failure:
            raise OidcError(f"{what} did not return JSON: {failure}") from failure
        if not isinstance(body, dict):
            raise OidcError(f"{what} returned {type(body).__name__}, not an object")
        return body

    # --- the flow ------------------------------------------------------------------------

    async def authorization_url(self, login: PendingLogin) -> str:
        """Where to send the browser, carrying this attempt's state, nonce and challenge."""
        discovered = await self.discovered()
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_url,
                "response_type": "code",
                "scope": " ".join(self._settings.oidc_scope_list),
                "state": login.state,
                "nonce": login.nonce,
                "code_challenge": challenge_for(login.code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in discovered.authorization_endpoint else "?"
        return f"{discovered.authorization_endpoint}{separator}{query}"

    async def exchange(self, code: str, code_verifier: str) -> str:
        """Trades the code for an ID token, over Apchi's own connection.

        The code is single-use and travelled through the browser; the verifier never left
        this process. Together they are what stops a code intercepted in a redirect from
        being redeemable by whoever intercepted it (RFC 7636 §1).
        """
        discovered = await self.discovered()
        secret = self._settings.oidc_client_secret or ""
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_url,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        auth: tuple[str, str] | None = None
        if discovered.secret_in_header:
            auth = (self.client_id, secret)
        else:
            form["client_secret"] = secret

        body = await self._json(
            "POST", discovered.token_endpoint, what="the code exchange", data=form, auth=auth
        )
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            # No ID token means the provider treated this as plain OAuth 2.0. Apchi has no
            # way to learn who signed in from an access token it cannot read, and guessing
            # from the userinfo endpoint would accept a token minted for something else.
            raise OidcError("the code exchange returned no id_token")
        return id_token

    # --- verification --------------------------------------------------------------------

    async def _key_set(self, refresh: bool = False) -> KeySet:
        if self._keys is None or refresh:
            discovered = await self.discovered()
            document = await self._json("GET", discovered.jwks_uri, what="the JWKS fetch")
            keys = document.get("keys")
            if not isinstance(keys, list):
                raise OidcError(f"{discovered.jwks_uri} returned no 'keys' array")
            serialization: KeySetSerialization = {"keys": keys}
            try:
                self._keys = KeySet.import_key_set(serialization)
            except (JoseError, ValueError, KeyError) as bad:
                raise OidcError(f"{discovered.jwks_uri} is not a usable JWKS: {bad}") from bad
        return self._keys

    async def claims(self, id_token: str, nonce: str) -> dict[str, Any]:
        """The ID token's claims, or `OidcError`.

        Four things are checked, and the first is the one that matters: the **signature**,
        against a key the provider published. Without it every other claim is whatever the
        browser chose to send. Then `iss`, `aud` and expiry, and then the `nonce` -- which is
        what ties this token to the login attempt Apchi started rather than to one somebody
        else did.
        """
        discovered = await self.discovered()
        algorithms = sorted(discovered.algorithms)
        try:
            token = jwt.decode(id_token, await self._key_set(), algorithms=algorithms)
        except JoseError:
            # A `kid` Apchi has not seen is the normal shape of key rotation, so the JWKS is
            # refetched once before the token is refused. Once, deliberately: a token signed
            # by a key that is not published is a forged token, and retrying on every one of
            # them turns a bad signature into a request Apchi makes to its provider.
            token = await self._retry_after_rotation(id_token, algorithms)

        registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": discovered.issuer},
            aud={"essential": True, "value": self.client_id},
            exp={"essential": True},
        )
        try:
            registry.validate(token.claims)
        except JoseError as invalid:
            raise OidcError(f"the id_token's claims are not acceptable: {invalid}") from invalid

        # Compared in constant time: the nonce is a secret Apchi minted, and a comparison
        # that returns early leaks it a character at a time to anybody who can retry.
        if not _matches(str(token.claims.get("nonce", "")), nonce):
            raise OidcError("the id_token's nonce is not the one this login asked for")
        return dict(token.claims)

    async def _retry_after_rotation(self, id_token: str, algorithms: list[str]) -> jwt.Token:
        try:
            return jwt.decode(id_token, await self._key_set(refresh=True), algorithms=algorithms)
        except JoseError as bad:
            raise OidcError(f"the id_token's signature is not valid: {bad}") from bad

    # --- what the claims mean ------------------------------------------------------------

    def username_in(self, claims: dict[str, Any]) -> str:
        """The person's Apchi username, from the configured claim.

        A missing claim fails the login. The alternative -- falling back to `sub` -- would
        record §20's audit under whatever opaque string the provider uses for a primary key
        (Dex returns `Cg0wLTM4NS0yODA4OS0wEgRtb2Nr`), which is an actor nobody can look up.
        """
        claim = self._settings.oidc_username_claim
        value = claims.get(claim)
        if not isinstance(value, str) or not value.strip():
            raise OidcError(
                f"the id_token has no usable {claim!r} claim, so there is no username to "
                f"record. Set APCHI_OIDC_USERNAME_CLAIM to a claim this provider sends."
            )
        return value.strip()

    def role_in(self, claims: dict[str, Any]) -> Role:
        """Admin when the role claim contains one of the configured groups, Operator otherwise.

        Operator is the default on purpose. The failure mode of a mapping that matches
        nothing should be a Cluster whose six Sections still work and whose `/admin/*` is
        closed -- not one where everybody can inject low-level configuration.
        """
        admin_groups = self._settings.admin_groups
        if not admin_groups:
            return "operator"
        groups = claims.get(self._settings.oidc_role_claim)
        # A single string as well as a list: providers disagree, and one that sends
        # "platform-admins" rather than ["platform-admins"] is not sending nothing.
        if isinstance(groups, str):
            present = {groups}
        elif isinstance(groups, list):
            present = {g for g in groups if isinstance(g, str)}
        else:
            present = set()
        return "admin" if present & admin_groups else "operator"


def _matches(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())
