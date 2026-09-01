"""Delivery of email-verification and password-reset messages."""

from __future__ import annotations

import hashlib
import html
import logging
from urllib.parse import urlencode

from app.domains.email.service import send_email

from .models import EmailTokenPurpose

logger = logging.getLogger(__name__)

_PATHS = {
    EmailTokenPurpose.VERIFY_EMAIL: "/verify-email",
    EmailTokenPurpose.RESET_PASSWORD: "/reset-password",
}

_CONTENT = {
    EmailTokenPurpose.VERIFY_EMAIL: {
        "subject": "Verify your Call Agent email",
        "heading": "Verify your email address",
        "body": "Confirm this address to finish setting up your Call Agent account.",
        "action": "Verify email",
    },
    EmailTokenPurpose.RESET_PASSWORD: {
        "subject": "Reset your Call Agent password",
        "heading": "Reset your password",
        "body": "Use this secure link to choose a new password for your account.",
        "action": "Reset password",
    },
}


def build_link(base_url: str, purpose: EmailTokenPurpose, raw_token: str) -> str:
    return f"{base_url}{_PATHS[purpose]}?{urlencode({'token': raw_token})}"


def deliver_email_token(
    *,
    email: str,
    purpose: EmailTokenPurpose,
    raw_token: str,
    base_url: str,
    resend_api_key: str,
    resend_from_email: str,
) -> str | None:
    link = build_link(base_url, purpose, raw_token)
    if not resend_api_key or not resend_from_email:
        logger.info("auth email (%s) for %s: %s", purpose.value, email, link)
        return None

    content = _CONTENT[purpose]
    safe_link = html.escape(link, quote=True)
    message_html = (
        f"<h1>{content['heading']}</h1>"
        f"<p>{content['body']}</p>"
        f'<p><a href="{safe_link}">{content["action"]}</a></p>'
        "<p>If you did not request this, you can ignore this email.</p>"
    )
    message_text = (
        f"{content['heading']}\n\n{content['body']}\n\n{link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    token_digest = hashlib.sha256(raw_token.encode()).hexdigest()
    return send_email(
        api_key=resend_api_key,
        sender=resend_from_email,
        recipient=email,
        subject=content["subject"],
        html=message_html,
        text=message_text,
        idempotency_key=f"auth-{purpose.value}-{token_digest}",
    )
