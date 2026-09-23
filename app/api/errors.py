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

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


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


class NotFound(ApchiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnprocessablePayload(ApchiError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "unprocessable_payload"


class UnknownConnector(UnprocessablePayload):
    code = "unknown_connector"


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
        return _render(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "unprocessable_payload",
            "The request payload is not valid.",
            details,
        )
