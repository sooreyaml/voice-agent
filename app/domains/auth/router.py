from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.domains.telephony.dependencies import ProvisioningProviderDep

from .constants import SESSION_COOKIE, SESSION_TTL
from .dependencies import (
    CurrentUser,
    CurrentUserDep,
    SettingsDep,
    StoreDep,
    request_origin,
)
from .exceptions import InvalidCredentials, NotFound
from .models import EmailTokenPurpose
from .notifications import deliver_email_token, deliver_email_verification_code
from .schemas import (
    EmailRequest,
    LoginRequest,
    MeResponse,
    MessageResponse,
    PasswordResetConfirmRequest,
    SignupRequest,
    SignupResponse,
    VerifyEmailRequest,
)
from .service import (
    authenticate,
    confirm_email_verification,
    hash_token,
    issue_email_token,
    issue_email_verification_code,
    issue_session,
    register,
    reset_password,
)

logger = logging.getLogger("callagent.auth")

router = APIRouter(tags=["auth"])


# -- helpers ---------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_session_cookie(response: Response, raw: str, settings: SettingsDep) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response, settings: SettingsDep) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _user_payload(user: dict[str, Any] | CurrentUser) -> dict[str, Any]:
    if isinstance(user, CurrentUser):
        return {
            "id": user.id,
            "email": user.email,
            "email_verified": user.email_verified,
            "is_platform_admin": user.is_platform_admin,
        }
    return {
        "id": str(user["id"]),
        "email": str(user["email"]),
        "email_verified": user["email_verified_at"] is not None,
        "is_platform_admin": bool(user["is_platform_admin"]),
    }


def _me_payload(store: StoreDep, user_id: str) -> dict[str, Any]:
    user = store.get_user(user_id)
    if user is None:  # pragma: no cover - session guarantees the row exists
        raise NotFound("User not found.")
    return {
        "user": _user_payload(user),
        "organizations": [
            {
                "id": str(row["id"]),
                "slug": str(row["slug"]),
                "name": str(row["name"]),
                "role": str(row["role"]),
            }
            for row in store.organizations_for_user(user_id)
        ],
    }


# -- sign up / in / out ------------------------------------------------


@router.get("/ping", summary="Liveness check that also seeds the CSRF cookie")
def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/auth/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its first organization",
    responses={409: {"description": "Email already registered"}},
)
def signup_route(
    body: SignupRequest,
    request: Request,
    response: Response,
    store: StoreDep,
    settings: SettingsDep,
    provisioning_provider: ProvisioningProviderDep,
) -> dict[str, Any]:
    billing_active = bool(settings.billing_enabled and settings.stripe_price_id)
    result = register(
        store,
        provisioning_provider,
        email=body.email,
        password=body.password,
        organization_name=body.organization_name,
        default_profile_template=settings.businesses_dir / "_default.yaml",
        default_timezone=settings.default_timezone,
        billing_active=billing_active,
        default_plan_code=settings.default_billing_plan_code,
        pool_country=settings.number_pool_country,
        number_type=settings.number_pool_number_type,
        sms_enabled=settings.number_pool_sms_enabled,
        bundle_sid=settings.number_pool_bundle_sid or None,
        address_sid=settings.number_pool_address_sid or None,
    )
    user = result.user
    organization = result.organization

    raw, _ = issue_session(
        store,
        str(user["id"]),
        secret=settings.auth_session_secret,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    _set_session_cookie(response, raw, settings)

    verify_code = issue_email_verification_code(
        store, str(user["id"]), secret=settings.auth_session_secret
    )
    deliver_email_verification_code(
        email=str(user["email"]),
        code=verify_code,
        resend_api_key=settings.resend_api_key,
        resend_from_email=settings.resend_from_email,
    )

    checkout_url: str | None = None
    subscription: dict[str, Any] | None = None
    if result.subscription_status is not None:
        subscription = {"status": result.subscription_status}
        # Local import: keeps billing out of the auth module's import graph.
        from app.domains.billing.provider import (
            StripeBillingError,
            StripeBillingService,
        )
        from app.domains.billing.services.subscriptions import start_signup_checkout

        provider = StripeBillingService(
            settings.stripe_secret_key, settings.stripe_webhook_secret
        )
        try:
            hosted = start_signup_checkout(
                store,
                provider,
                settings,
                organization_id=str(organization["id"]),
                user_email=str(user["email"]),
                base_url=settings.resolve_base_url(request_origin(request)),
            )
            checkout_url = hosted.url
            subscription["status"] = "checkout_pending"
        except (StripeBillingError, RuntimeError):
            logger.warning(
                "signup checkout could not be created for %s; the reaper will "
                "reclaim the number if it is never paid",
                organization["id"],
            )

    return {
        "user": _user_payload(user),
        "organization": {
            "id": str(organization["id"]),
            "slug": str(organization["slug"]),
            "name": str(organization["name"]),
        },
        "phone_number": result.phone_number,
        "subscription": subscription,
        "checkout_url": checkout_url,
    }


@router.post(
    "/auth/login",
    response_model=MeResponse,
    summary="Start a browser session",
    responses={401: {"description": "Bad email or password"}},
)
def login_route(
    body: LoginRequest,
    request: Request,
    response: Response,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    user = authenticate(store, body.email, body.password)
    if user is None:
        raise InvalidCredentials()
    raw, _ = issue_session(
        store,
        str(user["id"]),
        secret=settings.auth_session_secret,
        user_agent=request.headers.get("user-agent"),
        ip=_client_ip(request),
    )
    _set_session_cookie(response, raw, settings)
    return _me_payload(store, str(user["id"]))


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End the current browser session",
)
def logout_route(
    request: Request,
    response: Response,
    store: StoreDep,
    settings: SettingsDep,
) -> Response:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        store.revoke_session(hash_token(raw, settings.auth_session_secret))
    _clear_session_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeResponse, summary="The signed-in user")
def me_route(user: CurrentUserDep, store: StoreDep) -> dict[str, Any]:
    return _me_payload(store, user.id)


# -- email verification -------------------------------------------------


@router.post(
    "/auth/verify-email/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a fresh verification code",
)
def request_email_verification(
    user: CurrentUserDep,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, str]:
    if not user.email_verified:
        code = issue_email_verification_code(
            store, user.id, secret=settings.auth_session_secret
        )
        deliver_email_verification_code(
            email=user.email,
            code=code,
            resend_api_key=settings.resend_api_key,
            resend_from_email=settings.resend_from_email,
        )
    return {"message": "If the address needs verifying, a code is on its way."}


@router.post(
    "/auth/verify-email/confirm",
    response_model=MessageResponse,
    summary="Confirm the signed-in user's email with the code they were sent",
    responses={
        400: {"description": "Code is wrong or expired"},
        429: {"description": "Too many wrong codes; request a new one"},
    },
)
def confirm_email(
    body: VerifyEmailRequest,
    user: CurrentUserDep,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, str]:
    if not user.email_verified:
        confirm_email_verification(
            store, user.id, body.code, secret=settings.auth_session_secret
        )
    return {"message": "Email verified."}


# -- password reset ----------------------------------------------------


@router.post(
    "/auth/password-reset/request",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email a password-reset link if the account exists",
)
def request_password_reset(
    body: EmailRequest,
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
) -> dict[str, str]:
    user = store.get_user_by_email(body.email)
    base_url = settings.resolve_base_url(request_origin(request))
    if user is not None:
        token = issue_email_token(
            store,
            str(user["id"]),
            EmailTokenPurpose.RESET_PASSWORD,
            secret=settings.auth_session_secret,
        )
        deliver_email_token(
            email=str(user["email"]),
            purpose=EmailTokenPurpose.RESET_PASSWORD,
            raw_token=token,
            base_url=base_url,
            resend_api_key=settings.resend_api_key,
            resend_from_email=settings.resend_from_email,
        )
    # Identical response whether or not the address is registered.
    return {"message": "If that account exists, a reset link has been sent."}


@router.post(
    "/auth/password-reset/confirm",
    response_model=MessageResponse,
    summary="Set a new password from a reset token",
    responses={400: {"description": "Token invalid or expired"}},
)
def confirm_password_reset(
    body: PasswordResetConfirmRequest, store: StoreDep, settings: SettingsDep
) -> dict[str, str]:
    reset_password(
        store, body.token, body.password, secret=settings.auth_session_secret
    )
    return {"message": "Password updated. Sign in with your new password."}
