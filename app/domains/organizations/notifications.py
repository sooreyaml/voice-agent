"""Delivery of the organization-invitation email.

Logs the link for now; real transactional email is roadmap Phase 7. Tests
monkeypatch :func:`deliver_invitation` to capture the raw token.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_link(base_url: str, raw_token: str) -> str:
    return f"{base_url}/invite/{raw_token}"


def deliver_invitation(
    *, email: str, organization_name: str, role: str, raw_token: str, base_url: str
) -> None:
    logger.info(
        "invitation for %s to join %s as %s: %s",
        email,
        organization_name,
        role,
        build_link(base_url, raw_token),
    )
