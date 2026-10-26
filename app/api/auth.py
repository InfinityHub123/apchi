"""Signing in, signing out, and asking who you are.

These are the only routes that take no identity of their own: §19's authentication has to be
reachable by somebody who has none yet. `/auth/me` is the exception that answers in every
mode, including `none`, so a UI has one question to ask rather than a mode to special-case.
"""

import hmac
import logging
from datetime import timedelta

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    CallerDep,
    LoginStoreDep,
    OidcDep,
    SessionStoreDep,
    SettingsDep,
    UserStoreDep,
)
from app.api.errors import Unauthenticated
from app.auth.identity import Identity
from app.auth.logins import COOKIE as LOGIN_COOKIE
from app.auth.logins import pending
from app.auth.oidc import OidcError
from app.auth.sessions import COOKIE
from app.config import AuthMode, Environment, Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: One refusal for every way an SSO login can fail, for the same reason a password login has
#: one: the caller learns that it failed, and the log learns why.
SSO_REFUSED = "That SSO login could not be completed."

#: Told apart from SSO_REFUSED on purpose. "The provider could not be reached" is an Operator's
#: problem to fix and says nothing about any credential, where "that login failed" is about a
#: particular attempt and must not say which part of it was wrong.
SSO_UNAVAILABLE = "Apchi cannot reach its identity provider, so nobody can sign in right now."


def _sso_only(settings: Settings) -> None:
    if settings.auth_mode is not AuthMode.SSO:
        raise Unauthenticated(
            f"Apchi is running in {settings.auth_mode.value!r} mode, which has no identity "
            "provider."
        )


def _set_cookie(
    response: Response, name: str, value: str, max_age: int, settings: Settings
) -> None:
    """Both of Apchi's cookies, set the same way, so neither can drift from the other.

    `secure` only in the environments that are served over TLS: set unconditionally it would
    make local HTTP development impossible, and never set it would ship a credential that
    travels in clear text.
    """
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.environment in (Environment.PREP, Environment.PROD),
    )


class Credentials(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"username": "jo", "password": "the-password-goes-here"}]},
    )

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login", response_model=Identity, summary="Sign in with a password")
async def login(
    credentials: Credentials,
    response: Response,
    users: UserStoreDep,
    sessions: SessionStoreDep,
    settings: SettingsDep,
) -> Identity:
    """Sets a session cookie, and says who you now are.

    One refusal for every reason. "No such user", "wrong password" and "that account is
    disabled" are the same 401 with the same message, because telling them apart is how
    somebody enumerates the platform staff who have accounts here. The verification is paid
    even when the username is unknown, so the *timing* does not tell them either.
    """
    if settings.auth_mode is not AuthMode.PASSWORD:
        raise Unauthenticated(
            f"Apchi is running in {settings.auth_mode.value!r} mode, which has no passwords."
        )

    user = await users.authenticate(credentials.username, credentials.password)
    if user is None:
        raise Unauthenticated("That username and password do not match an enabled account.")

    value = await sessions.create(
        user.username, "password", timedelta(hours=settings.auth_session_hours)
    )
    _set_cookie(
        response, COOKIE, value, max_age=int(settings.auth_session_hours * 3600), settings=settings
    )
    logger.info("signed in", extra={"username": user.username, "role": user.role})
    return user.identity()


@router.get(
    "/sso/login",
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    response_class=RedirectResponse,
    summary="Start an SSO login",
    responses={307: {"description": "Redirect to the identity provider."}},
)
async def sso_login(
    logins: LoginStoreDep,
    provider: OidcDep,
    settings: SettingsDep,
) -> RedirectResponse:
    """Sends the browser to the provider, having first written down what the callback needs.

    The `state`, the `nonce` and the PKCE verifier are stored server-side and handed to the
    browser as one opaque cookie (`logins.py`). Nothing the browser carries can be read or
    altered by whoever holds it, and the row is deleted on the way back, which is what makes
    a replayed callback refusable.
    """
    _sso_only(settings)
    attempt = pending(timedelta(seconds=settings.oidc_login_seconds))
    # The authorization URL is built first: a provider that cannot be discovered must fail
    # the login, not leave a pending row and a cookie behind for a flow that never starts.
    #
    # A provider that is down, or settings that are incomplete, fail the login here -- they
    # never resolve to a caller. The reason goes to the log, which is where an Operator can
    # read it; a redirect that leaked why discovery failed would say it to the internet.
    try:
        url = await provider.authorization_url(attempt)
    except OidcError as failure:
        logger.warning("cannot start an sso login", extra={"reason": str(failure)})
        raise Unauthenticated(SSO_UNAVAILABLE) from failure
    handle = await logins.start(attempt)
    redirect = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    _set_cookie(
        redirect,
        LOGIN_COOKIE,
        handle,
        max_age=int(settings.oidc_login_seconds),
        settings=settings,
    )
    return redirect


@router.get(
    "/sso/callback",
    response_model=Identity,
    summary="Finish an SSO login",
)
async def sso_callback(
    request: Request,
    response: Response,
    logins: LoginStoreDep,
    users: UserStoreDep,
    sessions: SessionStoreDep,
    provider: OidcDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Identity:
    """Verifies the provider's answer and turns it into a session.

    Every refusal here is the same 401 with the same message, for the same reason as a
    password login: the detail goes to the log, where an Operator can read it, and not to
    whoever is driving the callback.
    """
    _sso_only(settings)
    attempt = await logins.take(request.cookies.get(LOGIN_COOKIE) or "")
    response.delete_cookie(LOGIN_COOKIE, httponly=True, samesite="lax")

    if error:
        # The provider itself refused -- consent declined, or a client it does not know.
        logger.warning("the identity provider refused the login", extra={"oidc_error": error})
        raise Unauthenticated(SSO_REFUSED)
    if attempt is None:
        # No cookie, an expired attempt, or a `state` that has already been spent. A replay
        # lands here because `take` deleted the row the first time round.
        logger.warning("sso callback without a live login attempt")
        raise Unauthenticated(SSO_REFUSED)
    # Encoded before comparing: `compare_digest` rejects a non-ASCII str outright, and
    # `state` is a query parameter anybody can put anything in.
    if not code or not state or not hmac.compare_digest(state.encode(), attempt.state.encode()):
        logger.warning("sso callback with a mismatched state")
        raise Unauthenticated(SSO_REFUSED)

    try:
        id_token = await provider.exchange(code, attempt.code_verifier)
        claims = await provider.claims(id_token, attempt.nonce)
        username = provider.username_in(claims)
        role = provider.role_in(claims)
    except OidcError as failure:
        logger.warning("sso login failed", extra={"reason": str(failure)})
        raise Unauthenticated(SSO_REFUSED) from failure

    user = await users.mirror(username, role)
    if user is None:
        raise Unauthenticated(SSO_REFUSED)

    value = await sessions.create(
        user.username, "sso", timedelta(hours=settings.auth_session_hours)
    )
    _set_cookie(
        response,
        COOKIE,
        value,
        max_age=int(settings.auth_session_hours * 3600),
        settings=settings,
    )
    logger.info("signed in", extra={"username": user.username, "role": user.role, "via": "sso"})
    return user.identity("sso")


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End this session",
)
async def logout(
    request: Request, response: Response, sessions: SessionStoreDep, identity: CallerDep
) -> None:
    """Deletes the session row, so it stops working on the next request rather than at expiry.

    That immediacy is the whole reason sessions are rows rather than signed cookies: clearing
    the browser's copy alone would leave a credential that still works to whoever kept it.
    """
    del identity  # resolving it is what proves there is a session to end.
    if value := request.cookies.get(COOKIE):
        await sessions.delete(value)
    response.delete_cookie(COOKIE, httponly=True, samesite="lax")


@router.get("/me", response_model=Identity, summary="Who Apchi thinks you are")
async def me(identity: CallerDep) -> Identity:
    """Answers in every mode, `none` included, where it reports the anonymous Admin.

    A UI asks this once instead of branching on the configured mode, and an Operator reading
    it can tell a 403 they expected from one they did not.
    """
    return identity
