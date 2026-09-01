"""Delivery of organization-invitation emails."""

from __future__ import annotations

import hashlib
import html
import logging

from app.domains.email.service import send_email

logger = logging.getLogger(__name__)


def build_link(base_url: str, raw_token: str) -> str:
    return f"{base_url}/invite/{raw_token}"


def deliver_invitation(
    *,
    email: str,
    organization_name: str,
    role: str,
    raw_token: str,
    base_url: str,
    resend_api_key: str,
    resend_from_email: str,
) -> str | None:
    link = build_link(base_url, raw_token)
    if not resend_api_key or not resend_from_email:
        logger.info(
            "invitation for %s to join %s as %s: %s",
            email,
            organization_name,
            role,
            link,
        )
        return None

    organization_name = " ".join(organization_name.split()) or "the organization"
    safe_organization = html.escape(organization_name)
    safe_role = html.escape(role)
    safe_link = html.escape(link, quote=True)
    message_html = (
        f"<h1>Join {safe_organization}</h1>"
        f"<p>You have been invited to join {safe_organization} as {safe_role}.</p>"
        f'<p><a href="{safe_link}">Accept invitation</a></p>'
        "<p>If you were not expecting this invitation, you can ignore this email.</p>"
    )
    message_text = (
        f"Join {organization_name}\n\n"
        f"You have been invited to join {organization_name} as {role}.\n\n"
        f"{link}\n\n"
        "If you were not expecting this invitation, you can ignore this email."
    )
    token_digest = hashlib.sha256(raw_token.encode()).hexdigest()
    return send_email(
        api_key=resend_api_key,
        sender=resend_from_email,
        recipient=email,
        subject=f"You're invited to join {organization_name} on Call Agent",
        html=message_html,
        text=message_text,
        idempotency_key=f"invitation-{token_digest}",
    )
