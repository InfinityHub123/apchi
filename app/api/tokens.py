"""Tokens an Operator mints for their automations.

Everything here is scoped to the caller: you mint your own, you list your own, you revoke your
own. A token acts as its owner, so somebody else's token is somebody else's access, and an
endpoint that let one person manage another's would be a way to take over an account without
knowing a password.
"""

import logging

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Authenticated, CallerDep, SettingsDep, TokenStoreDep
from app.api.errors import NotFound, UnprocessablePayload, bad_cursor
from app.api.pages import DEFAULT_LIMIT, BadCursor, BeforeParam, LimitParam, Page
from app.auth.identity import ANONYMOUS
from app.auth.tokens import Minted, Token
from app.config import AuthMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tokens", tags=["tokens"], dependencies=[Authenticated])


class TokenRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "ci-pipeline", "expires_in_days": 90}]},
    )

    name: str = Field(
        min_length=1,
        max_length=128,
        description="What this token is for, so its owner can tell theirs apart later.",
    )
    expires_in_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description=(
            "Omit for a token that does not expire, which is a real choice for a pipeline "
            "rather than an oversight -- revoking it is one request."
        ),
    )


@router.post(
    "",
    response_model=Minted,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a token for an automation",
)
async def create_token(
    request: TokenRequest,
    identity: CallerDep,
    tokens: TokenStoreDep,
    settings: SettingsDep,
) -> Minted:
    """The response carries the secret, and nothing ever will again.

    Apchi stores a digest of it, so a token that is lost is replaced rather than looked up.
    The token acts as **you**: it takes your role on every request it makes, so it loses
    access when you do.
    """
    if settings.auth_mode is AuthMode.NONE or identity.subject == ANONYMOUS:
        raise UnprocessablePayload(
            "There is nobody to own a token: Apchi is running in 'none' mode, where every "
            "caller is anonymous. Configure an authentication mode first.",
            details=[{"property": "auth_mode", "problem": "no authenticated user exists"}],
        )
    return await tokens.create(identity.subject, request.name, request.expires_in_days)


@router.get("", response_model=Page[Token], summary="Your tokens")
async def list_tokens(
    identity: CallerDep,
    tokens: TokenStoreDep,
    limit: LimitParam = DEFAULT_LIMIT,
    before: BeforeParam = None,
) -> Page[Token]:
    """Metadata only, and only yours. Newest first.

    Revoked and expired ones are listed too: "what has been running as me, and what stopped"
    is the question this answers, and hiding the dead ones would leave an Operator unable to
    tell a token they revoked from one that never existed.
    """
    try:
        return await tokens.owned_by(identity.subject, limit=limit, before=before)
    except BadCursor as bad:
        raise bad_cursor() from bad


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of your tokens",
)
async def revoke_token(token_id: str, identity: CallerDep, tokens: TokenStoreDep) -> None:
    """Immediate: the next request carrying it is refused.

    A token somebody else owns is reported as **not found** rather than forbidden. Saying
    "that is not yours" would confirm the id exists, which is the one thing an id is worth
    knowing.
    """
    token = await tokens.get(token_id)
    if token is None or token.owner != identity.subject:
        raise NotFound(f"You have no token {token_id!r}.")
    await tokens.revoke(token_id)
    logger.info("token revoked", extra={"owner": identity.subject, "token": token_id})
