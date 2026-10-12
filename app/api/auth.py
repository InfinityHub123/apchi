"""Signing in, signing out, and asking who you are.

These are the only routes that take no identity of their own: §19's authentication has to be
reachable by somebody who has none yet. `/auth/me` is the exception that answers in every
mode, including `none`, so a UI has one question to ask rather than a mode to special-case.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CallerDep, SessionStoreDep, SettingsDep, UserStoreDep
from app.api.errors import Unauthenticated
from app.auth.identity import Identity
from app.auth.sessions import COOKIE
from app.config import AuthMode, Environment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
        user.username, user.role, timedelta(hours=settings.auth_session_hours)
    )
    response.set_cookie(
        COOKIE,
        value,
        max_age=int(settings.auth_session_hours * 3600),
        httponly=True,
        samesite="lax",
        # Secure in the environments that are served over TLS. Set unconditionally it would
        # make local HTTP development impossible; never set it would ship a cookie that
        # travels in clear text.
        secure=settings.environment in (Environment.PREP, Environment.PROD),
    )
    logger.info("signed in", extra={"username": user.username, "role": user.role})
    return user.identity()


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
