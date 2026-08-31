"""Delivery of auth emails.

Real transactional email is roadmap Phase 7. Until then this logs the action and
the link so local development and admin-led onboarding can complete the flow.
Tests monkeypatch :func:`deliver_email_token` to capture the raw token.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from .models import EmailTokenPurpose

logger = logging.getLogger(__name__)

_PATHS = {
    EmailTokenPurpose.VERIFY_EMAIL: "/verify-email",
    EmailTokenPurpose.RESET_PASSWORD: "/reset-password",
}


def build_link(base_url: str, purpose: EmailTokenPurpose, raw_token: str) -> str:
    return f"{base_url}{_PATHS[purpose]}?{urlencode({'token': raw_token})}"


def deliver_email_token(
    *, email: str, purpose: EmailTokenPurpose, raw_token: str, base_url: str
) -> None:
    link = build_link(base_url, purpose, raw_token)
    logger.info("auth email (%s) for %s: %s", purpose.value, email, link)
