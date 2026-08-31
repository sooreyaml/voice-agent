"""Auth business logic: token hashing, session issue, signup, email tokens.

Functions here are deliberately free of FastAPI types so they can be unit tested
and reused from scripts. Anything web-shaped (cookies, requests) stays in
``router.py`` / ``dependencies.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime

from app.store import Store

from .constants import (
    DEV_TOKEN_KEY,
    RESET_PASSWORD_TTL,
    SESSION_TTL,
    VERIFY_EMAIL_TTL,
)
from .exceptions import EmailTaken, InvalidToken
from .models import EmailTokenPurpose
from .passwords import hash_password, verify_password

logger = logging.getLogger(__name__)


def token_key(secret: str) -> bytes:
    if secret:
        return secret.encode("utf-8")
    logger.warning(
        "AUTH_SESSION_SECRET is unset; using an insecure development key. "
        "Set it before staging or production."
    )
    return DEV_TOKEN_KEY.encode("utf-8")


def hash_token(raw: str, secret: str) -> str:
    """Keyed hash stored in place of a raw session / email token."""
    return hmac.new(token_key(secret), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def new_raw_token() -> str:
    return secrets.token_urlsafe(32)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100].rstrip("-")


def unique_org_slug(store: Store, name: str) -> str:
    base = slugify(name) or "organization"
    slug = base
    suffix = 2
    while store.organization_id_for_slug(slug) is not None:
        ending = f"-{suffix}"
        slug = f"{base[: 100 - len(ending)].rstrip('-')}{ending}"
        suffix += 1
    return slug


# -- sessions -----------------------------------------------------------------


def issue_session(
    store: Store,
    user_id: str,
    *,
    secret: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, datetime]:
    raw = new_raw_token()
    expires_at = _utcnow() + SESSION_TTL
    store.create_session(
        user_id, hash_token(raw, secret), expires_at, user_agent, ip
    )
    return raw, expires_at


def authenticate(store: Store, email: str, password: str) -> dict | None:
    user = store.get_user_by_email(normalize_email(email))
    if user is None:
        # Keep timing similar whether or not the account exists.
        verify_password(password, None)
        return None
    if not verify_password(password, user.get("password_hash")):
        return None
    return user


# -- signup -----------------------------------------------------------------


def signup(
    store: Store, *, email: str, password: str, organization_name: str
) -> tuple[dict, dict]:
    email = normalize_email(email)
    if store.get_user_by_email(email) is not None:
        raise EmailTaken()
    user_id = store.create_user(email, password_hash=hash_password(password))
    slug = unique_org_slug(store, organization_name)
    org_id = store.create_organization(slug, organization_name.strip())
    store.add_membership(org_id, user_id, "owner")
    user = store.get_user(user_id)
    organization = store.organization(org_id)
    assert user is not None and organization is not None
    return user, organization


# -- email verification / password reset ----------------------------------


def issue_email_token(
    store: Store, user_id: str, purpose: EmailTokenPurpose, *, secret: str
) -> str:
    raw = new_raw_token()
    ttl = (
        VERIFY_EMAIL_TTL
        if purpose is EmailTokenPurpose.VERIFY_EMAIL
        else RESET_PASSWORD_TTL
    )
    store.create_email_token(
        user_id, purpose.value, hash_token(raw, secret), _utcnow() + ttl
    )
    return raw


def consume_email_token(
    store: Store, raw: str, purpose: EmailTokenPurpose, *, secret: str
) -> str:
    user_id = store.consume_email_token(purpose.value, hash_token(raw, secret))
    if user_id is None:
        raise InvalidToken()
    return user_id


def confirm_email_verification(store: Store, raw: str, *, secret: str) -> str:
    user_id = consume_email_token(
        store, raw, EmailTokenPurpose.VERIFY_EMAIL, secret=secret
    )
    store.mark_email_verified(user_id)
    return user_id


def reset_password(store: Store, raw: str, new_password: str, *, secret: str) -> str:
    user_id = consume_email_token(
        store, raw, EmailTokenPurpose.RESET_PASSWORD, secret=secret
    )
    store.set_user_password(user_id, hash_password(new_password))
    # A reset is also a "log out everywhere" — old sessions must not survive it.
    store.revoke_all_user_sessions(user_id)
    return user_id
