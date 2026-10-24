"""Problem responses for /v1; legacy read response bodies are unchanged."""

from http import HTTPStatus
from uuid import uuid4

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, headers=None):
        self.status, self.code, self.detail = status, code, detail
        self.headers = headers or {}


class ProblemDocument(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str


def is_versioned(path: str) -> bool:
    return path == "/v1" or path.startswith("/v1/") or path.startswith("/internal/v1/")


def problem_response(request: Request, exc: Problem):
    return JSONResponse(
        status_code=exc.status,
        media_type="application/problem+json",
        headers=exc.headers,
        content={
            "type": f"urn:finance:problem:{exc.code.lower().replace('_', '-')}",
            "title": HTTPStatus(exc.status).phrase,
            "status": exc.status,
            "detail": exc.detail,
            "code": exc.code,
            "request_id": getattr(request.state, "request_id", uuid4().hex),
        },
    )


def install_problem_handlers(app):
    app.add_exception_handler(Problem, problem_response)

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, exc: HTTPException):
        if not is_versioned(request.url.path):
            return await http_exception_handler(request, exc)
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return problem_response(request, Problem(
            exc.status_code, codes.get(exc.status_code, "REQUEST_FAILED"),
            HTTPStatus(exc.status_code).phrase, exc.headers,
        ))

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        if not is_versioned(request.url.path):
            return await request_validation_exception_handler(request, exc)
        # Do not echo user inputs, cookies, credentials, or database diagnostics.
        return problem_response(request, Problem(422, "INVALID_REQUEST", "Request validation failed"))

    @app.exception_handler(SQLAlchemyError)
    async def handle_database(request: Request, exc: SQLAlchemyError):
        if not is_versioned(request.url.path):
            return PlainTextResponse("Internal Server Error", status_code=500)
        return problem_response(request, Problem(
            503, "DATABASE_UNAVAILABLE",
            "The API database is unavailable or requires migrations",
            {"Retry-After": "5"},
        ))


PROBLEM_RESPONSES = {
    status: {
        "description": HTTPStatus(status).phrase,
        "content": {"application/problem+json": {"schema": ProblemDocument.schema()}},
    }
    for status in (400, 401, 403, 404, 409, 412, 422, 428, 429, 503)
}


class RequestIDMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode("ascii"))
                ]
                private = ("/v1/games", "/v1/me", "/v1/guest-sessions", "/v1/authentication",
                           "/v1/oidc", "/v1/oidc-authentications", "/v1/guest-claims",
                           "/v1/sessions", "/v1/session-revocations")
                if any(scope["path"] == path or scope["path"].startswith(path + "/") for path in private):
                    message["headers"] = [
                        (key, value) for key, value in message["headers"] if key.lower() != b"cache-control"
                    ] + [(b"cache-control", b"no-store")]
                if scope["path"].startswith(("/v1/oidc", "/v1/guest-claims")):
                    message["headers"] += [(b"referrer-policy", b"no-referrer")]
            await send(message)

        await self.app(scope, receive, send_with_id)
