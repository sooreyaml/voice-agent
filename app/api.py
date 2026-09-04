"""Wires the versioned management API onto the FastAPI app.

Everything under ``/api/v1`` shares one JSON error envelope
(``{"error": {"code", "message", "field_errors", "request_id"}}``), a
per-request id, an always-present CSRF cookie, and double-submit CSRF checks on
authenticated mutations. The public call webhook and ``/health`` are untouched.
"""

from __future__ import annotations

import hmac
import http
import logging
import secrets
import uuid

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .domains.api_keys.router import router as api_keys_router
from .domains.auth.constants import CSRF_COOKIE, CSRF_HEADER, ErrorCode
from .domains.auth.exceptions import APIError, CsrfFailed
from .domains.auth.router import router as auth_router
from .domains.billing.routers.management import router as billing_router
from .domains.billing.routers.spend import router as spend_limit_router
from .domains.billing.routers.webhooks import router as billing_webhook_router
from .domains.businesses.router import router as agent_router
from .domains.integrations.router import router as integrations_router
from .domains.onboarding.router import router as onboarding_router
from .domains.organizations.operations_router import router as operations_router
from .domains.organizations.router import router as organizations_router
from .domains.privacy.router import router as privacy_router
from .domains.webhooks.router import router as webhooks_router

logger = logging.getLogger("callagent.api")

API_PREFIX = "/api/v1"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_TOKEN_TTL = 60 * 60 * 24 * 14

# Human-readable fallback for statuses that reach the API without a typed
# APIError: raw HTTPExceptions, framework 404/405 responses, and unexpected
# errors. The endpoint's own ``detail`` still wins when it is real sentence
# copy rather than a stock HTTP reason phrase ("Not Found", "Method Not
# Allowed", ...).
_STATUS_COPY: dict[int, tuple[str, str]] = {
    400: (ErrorCode.BAD_REQUEST, "We couldn't process that request. Check the details and try again."),
    401: (ErrorCode.NOT_AUTHENTICATED, "Sign in to continue."),
    403: (ErrorCode.FORBIDDEN, "You do not have access to this."),
    404: (ErrorCode.NOT_FOUND, "We couldn't find what you were looking for."),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "That action isn't supported here."),
    409: (ErrorCode.CONFLICT, "That conflicts with the current state. Refresh and try again."),
    413: (ErrorCode.PAYLOAD_TOO_LARGE, "That request is too large."),
    415: (ErrorCode.UNSUPPORTED_MEDIA_TYPE, "That format isn't supported."),
    429: (ErrorCode.RATE_LIMITED, "Too many requests. Wait a moment and try again."),
    500: (ErrorCode.INTERNAL_ERROR, "Something went wrong on our end. We've logged it — please try again."),
    502: (ErrorCode.SERVICE_UNAVAILABLE, "An upstream service is unavailable right now. Please try again shortly."),
    503: (ErrorCode.SERVICE_UNAVAILABLE, "The service is temporarily unavailable. Please try again shortly."),
    504: (ErrorCode.UPSTREAM_TIMEOUT, "That request took too long. Please try again."),
}
_INTERNAL_ERROR = _STATUS_COPY[500]


def _status_copy(status_code: int) -> tuple[str, str]:
    if status_code in _STATUS_COPY:
        return _STATUS_COPY[status_code]
    if 500 <= status_code <= 599:
        return _INTERNAL_ERROR
    return ErrorCode.INTERNAL_ERROR, _INTERNAL_ERROR[1]


def _friendly_message(status_code: int, detail: object) -> str:
    """Prefer a real sentence from the endpoint; fall back to friendly copy."""
    _, fallback = _status_copy(status_code)
    if not isinstance(detail, str) or not detail.strip():
        return fallback
    try:
        stock_phrase = http.HTTPStatus(status_code).phrase
    except ValueError:
        stock_phrase = ""
    # Starlette fills unset details with the stock reason phrase ("Not Found",
    # "Method Not Allowed") -- not something to show a user.
    return fallback if detail == stock_phrase else detail


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    return existing or f"req_{uuid.uuid4().hex[:24]}"


def _envelope(
    status_code: int, code: str, message: str, request_id: str, *, fields=None
):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "field_errors": fields or {},
                "request_id": request_id,
            }
        },
    )


def install_api(app: FastAPI) -> None:
    app.include_router(auth_router, prefix=API_PREFIX)
    # Billing routers are always registered; each 404s while BILLING_ENABLED is
    # off (the default) via a router-level dependency.
    app.include_router(billing_router, prefix=API_PREFIX)
    app.include_router(spend_limit_router, prefix=API_PREFIX)
    app.include_router(billing_webhook_router)
    app.include_router(organizations_router, prefix=API_PREFIX)
    app.include_router(onboarding_router, prefix=API_PREFIX)
    app.include_router(agent_router, prefix=API_PREFIX)
    app.include_router(operations_router, prefix=API_PREFIX)
    app.include_router(privacy_router, prefix=API_PREFIX)
    app.include_router(webhooks_router, prefix=API_PREFIX)
    app.include_router(integrations_router, prefix=API_PREFIX)
    app.include_router(api_keys_router, prefix=API_PREFIX)

    @app.middleware("http")
    async def api_envelope_and_csrf(request: Request, call_next):
        request.state.request_id = (
            request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:24]}"
        )
        is_api = request.url.path.startswith(API_PREFIX)

        # Double-submit CSRF on every state-changing API call. Clients obtain the
        # cookie from any GET (e.g. /api/v1/ping) and echo it in the header.
        # Bearer API-key requests carry no ambient cookie, so CSRF does not apply.
        bearer_auth = request.headers.get("authorization", "")[:7].lower() == "bearer "
        if is_api and not bearer_auth and request.method not in SAFE_METHODS:
            cookie = request.cookies.get(CSRF_COOKIE)
            header = request.headers.get(CSRF_HEADER)
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                exc = CsrfFailed()
                return _envelope(
                    exc.status_code, exc.code, exc.message, request.state.request_id
                )

        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id

        if is_api and CSRF_COOKIE not in request.cookies:
            settings = getattr(app.state, "settings", None)
            secure = bool(settings.cookie_secure) if settings else True
            response.set_cookie(
                CSRF_COOKIE,
                secrets.token_urlsafe(32),
                max_age=CSRF_TOKEN_TTL,
                httponly=False,
                secure=secure,
                samesite="lax",
                path="/",
            )
        return response

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.envelope(_request_id(request)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith(API_PREFIX):
            return await request_validation_exception_handler(request, exc)
        fields: dict[str, str] = {}
        for err in exc.errors():
            path = [
                str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")
            ]
            fields[".".join(path) or "body"] = err.get("msg", "Invalid value.")
        return _envelope(
            422,
            ErrorCode.VALIDATION_FAILED,
            "Some fields need fixing.",
            _request_id(request),
            fields=fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exc(request: Request, exc: StarletteHTTPException):
        if not request.url.path.startswith(API_PREFIX):
            return await http_exception_handler(request, exc)
        code, _ = _status_copy(exc.status_code)
        message = _friendly_message(exc.status_code, exc.detail)
        response = _envelope(exc.status_code, code, message, _request_id(request))
        if exc.headers:
            # Preserve semantically required headers (Allow on 405,
            # WWW-Authenticate on 401, Retry-After on 429).
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        request_id = _request_id(request)
        logger.exception(
            "unhandled error on %s %s [request_id=%s]",
            request.method,
            request.url.path,
            request_id,
        )
        if not request.url.path.startswith(API_PREFIX):
            # Keep Starlette's default off the API (stack trace in dev, plain
            # 500 otherwise); only the JSON API gets the shared envelope.
            raise exc
        code, message = _INTERNAL_ERROR
        return _envelope(500, code, message, request_id)
