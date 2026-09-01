"""Wires the versioned management API onto the FastAPI app.

Everything under ``/api/v1`` shares one JSON error envelope
(``{"error": {"code", "message", "field_errors", "request_id"}}``), a
per-request id, an always-present CSRF cookie, and double-submit CSRF checks on
authenticated mutations. The public call webhook and ``/health`` are untouched.
"""

from __future__ import annotations

import hmac
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
from .domains.integrations.router import router as integrations_router
from .domains.onboarding.router import router as onboarding_router
from .domains.onboarding.router import (
    self_service_router as self_service_onboarding_router,
)
from .domains.organizations.operations_router import router as operations_router
from .domains.organizations.router import router as organizations_router
from .domains.privacy.router import router as privacy_router
from .domains.telephony.router import router as telephony_router
from .domains.telephony.router import (
    self_service_router as self_service_telephony_router,
)
from .domains.webhooks.router import router as webhooks_router

logger = logging.getLogger("callagent.api")

API_PREFIX = "/api/v1"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_TOKEN_TTL = 60 * 60 * 24 * 14


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


def install_api(app: FastAPI, *, billing_enabled: bool = True) -> None:
    app.include_router(auth_router, prefix=API_PREFIX)
    if billing_enabled:
        app.include_router(billing_router, prefix=API_PREFIX)
        app.include_router(spend_limit_router, prefix=API_PREFIX)
        app.include_router(billing_webhook_router)
    app.include_router(organizations_router, prefix=API_PREFIX)
    app.include_router(operations_router, prefix=API_PREFIX)
    app.include_router(privacy_router, prefix=API_PREFIX)
    app.include_router(onboarding_router, prefix=API_PREFIX)
    app.include_router(self_service_onboarding_router, prefix=API_PREFIX)
    app.include_router(telephony_router, prefix=API_PREFIX)
    app.include_router(self_service_telephony_router, prefix=API_PREFIX)
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
        code = {
            401: ErrorCode.NOT_AUTHENTICATED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _envelope(exc.status_code, code, message, _request_id(request))
