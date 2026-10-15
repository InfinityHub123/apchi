"""The structured error model.

The code is what clients branch on. A status cannot carry the difference between
an unknown connector and a missing reference -- both are 422 -- so the machine
readable code is the contract, and the status is a coarse signal.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import PyMongoError

from app.audit.actor import actor
from app.audit.changes import category_of
from app.audit.events import AuditStore

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

#: The methods that were trying to change something. A refused GET is a read that found
#: nothing, which the log already carries and §20 has no actor question about.
_MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ApiError(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = []
    request_id: str


class ApchiError(Exception):
    """Base for errors that carry a machine-readable code."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "error"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []


class Conflict(ApchiError):
    """The payload is fine; the world is not."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class NameAlreadyTaken(Conflict):
    code = "name_already_taken"


class MaintenanceModeEngaged(Conflict):
    """Operator mutations are disabled. Reads are unaffected."""

    code = "maintenance_mode"


class Unauthenticated(ApchiError):
    """No usable credential. Distinct from Forbidden: this one is answered by logging in."""

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class Forbidden(ApchiError):
    """A known caller without the role for this route. Logging in again will not help."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class NotFound(ApchiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnprocessablePayload(ApchiError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "unprocessable_payload"


class UnknownConnector(UnprocessablePayload):
    code = "unknown_connector"


class CandidateTooLarge(UnprocessablePayload):
    """The Candidate would not fit in one MongoDB document.

    Its own code because it is the one refusal an Operator cannot fix by correcting the
    request: the payload is fine and the Cluster is fine, and what is wrong is the total.
    A client that can branch on this can tell the Operator to remove something rather than
    retry.
    """

    code = "candidate_too_large"


def bad_cursor() -> UnprocessablePayload:
    """One refusal for a `before` that did not come from a previous page, so every paginated
    collection says the same thing about the same mistake (§19).

    Here rather than in `api/pages` because that module must not import this one: `errors`
    reaches `audit.events` through the audit category table, and `audit.events` pages its own
    history -- so the import would close a cycle.
    """
    return UnprocessablePayload(
        "That `before` value did not come from a previous page's `next`. Omit it to start "
        "from the newest entries.",
        details=[{"property": "before", "problem": "not a cursor this endpoint issued"}],
    )


def request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    generated = uuid.uuid4().hex
    request.state.request_id = generated
    return generated


def _render(
    request: Request, status_code: int, code: str, message: str, details: list[dict[str, Any]]
) -> JSONResponse:
    rid = request_id(request)
    body = ApiError(code=code, message=message, details=details, request_id=rid)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers={REQUEST_ID_HEADER: rid},
    )


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request_id(request)
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, rid)
        return response

    @app.exception_handler(ApchiError)
    async def _apchi_error(request: Request, exc: ApchiError) -> JSONResponse:
        logger.info("request rejected", extra={"code": exc.code, "reason": exc.message})
        await _record_refusal(request, exc.message)
        return _render(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI returns 422 for a malformed body as well as a well-formed one that
        # fails validation. RFC 9110 would put the former at 400; reclaiming that
        # distinction costs an exception handler for something no client branches on.
        details = [
            {"field": ".".join(str(p) for p in error["loc"][1:]), "problem": error["msg"]}
            for error in exc.errors()
        ]
        # Recorded like any other refusal (§20). One rule -- a refused mutation is recorded --
        # rather than one with an exception for which validator refused it: "jo tried to
        # change this and was told no" is the same answer whether a gate or a schema said so,
        # and an audit that quietly skipped malformed payloads would be a gap shaped exactly
        # like somebody probing the API.
        await _record_refusal(request, "The request payload is not valid.")
        return _render(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "unprocessable_payload",
            "The request payload is not valid.",
            details,
        )


async def _record_refusal(request: Request, reason: str) -> None:
    """Record a refused mutation (§20), from the one place every refusal passes through.

    "Who tried to change this during the freeze" is a question somebody asks, and a trail
    that only holds what succeeded cannot answer it. Written here rather than in each gate
    because there is more than one way to be refused -- a frozen Candidate, Maintenance Mode,
    a payload that failed validation -- and they arrive at this handler alike.

    The category comes from the route's own tag (`app.audit.changes`), because a refusal never
    reached the Candidate and so left no diff to derive anything from.

    A failed audit write is logged and swallowed *here only*: the operation has already been
    refused, and turning its 409 into a 500 would replace a clear answer with a confusing one.
    On the success path the opposite holds, and `CandidateStore.save` lets it propagate -- a
    change that stored but was not recorded must not report success.
    """
    if request.method not in _MUTATIONS:
        return
    route = request.scope.get("route")
    category = category_of(list(getattr(route, "tags", [])))
    if category is None:
        return
    who = actor()
    store: AuditStore | None = getattr(request.app.state, "audit_store", None)
    if store is None:
        return
    try:
        await store.record(
            who.subject,
            who.via,
            category,
            "attempted",
            resource=_resource_in(request),
            outcome="refused",
            detail=reason,
            token_id=who.token_id,
        )
    except PyMongoError:
        logger.exception("could not record a refused mutation in the audit history")


def _resource_in(request: Request) -> str | None:
    """The resource a path names, when it names one.

    Taken from the matched path parameters rather than parsed out of the URL, so it is
    whatever the route itself called it and not a guess at which segment mattered.
    """
    params: dict[str, Any] = request.scope.get("path_params") or {}
    for key in ("name", "resource", "key", "token_id", "path"):
        if key in params:
            return str(params[key])
    return None
