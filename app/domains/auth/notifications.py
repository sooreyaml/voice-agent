"""Delivery of email-verification codes and password-reset links."""

from __future__ import annotations

import hashlib
import html
import logging
from urllib.parse import urlencode

from app.domains.email.service import send_email

from .models import EmailTokenPurpose

logger = logging.getLogger(__name__)

_RESET_PATH = "/reset-password"


def build_link(base_url: str, purpose: EmailTokenPurpose, raw_token: str) -> str:
    # Only password reset is a link now; verification is a typed code.
    return f"{base_url}{_RESET_PATH}?{urlencode({'token': raw_token})}"


def deliver_email_verification_code(
    *,
    email: str,
    code: str,
    resend_api_key: str,
    resend_from_email: str,
) -> str | None:
    """Email the 6-digit verification code. Returns the Resend message id, or
    ``None`` when delivery is not configured (the code is logged instead)."""
    if not resend_api_key or not resend_from_email:
        logger.info("email verification code for %s: %s", email, code)
        return None

    safe_code = html.escape(code)
    message_html = (
        "<h1>Confirm your email address</h1>"
        "<p>Enter this code to finish setting up your Call Agent account:</p>"
        f"<p style=\"font-size:24px;font-weight:700;letter-spacing:4px\">{safe_code}</p>"
        "<p>The code expires in 15 minutes. If you did not request this, you "
        "can ignore this email.</p>"
    )
    message_text = (
        "Confirm your email address\n\n"
        f"Enter this code to finish setting up your Call Agent account: {code}\n\n"
        "The code expires in 15 minutes. If you did not request this, you can "
        "ignore this email."
    )
    code_digest = hashlib.sha256(code.encode()).hexdigest()[:16]
    return send_email(
        api_key=resend_api_key,
        sender=resend_from_email,
        recipient=email,
        subject="Your Call Agent verification code",
        html=message_html,
        text=message_text,
        idempotency_key=f"verify-email-code-{code_digest}",
    )


def deliver_email_token(
    *,
    email: str,
    purpose: EmailTokenPurpose,
    raw_token: str,
    base_url: str,
    resend_api_key: str,
    resend_from_email: str,
) -> str | None:
    """Email a password-reset link (``purpose`` is always
    ``RESET_PASSWORD``)."""
    link = build_link(base_url, purpose, raw_token)
    if not resend_api_key or not resend_from_email:
        logger.info("auth email (%s) for %s: %s", purpose.value, email, link)
        return None

    safe_link = html.escape(link, quote=True)
    message_html = (
        "<h1>Reset your password</h1>"
        "<p>Use this secure link to choose a new password for your account.</p>"
        f'<p><a href="{safe_link}">Reset password</a></p>'
        "<p>If you did not request this, you can ignore this email.</p>"
    )
    message_text = (
        "Reset your password\n\n"
        "Use this secure link to choose a new password for your account.\n\n"
        f"{link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    token_digest = hashlib.sha256(raw_token.encode()).hexdigest()
    return send_email(
        api_key=resend_api_key,
        sender=resend_from_email,
        recipient=email,
        subject="Reset your Call Agent password",
        html=message_html,
        text=message_text,
        idempotency_key=f"auth-{purpose.value}-{token_digest}",
    )
