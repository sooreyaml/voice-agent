"""Organization profile, membership, and invitation logic (plus audit writes).

FastAPI-free so the rules stay unit-testable. The router supplies the
``OrgContext`` (already membership-checked), the token secret, and request
metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.constants import INVITATION_TTL, ROLE_RANK
from app.domains.auth.dependencies import CurrentUser, OrgContext
from app.domains.auth.exceptions import Forbidden
from app.domains.auth.service import hash_token, new_raw_token, unique_org_slug
from app.store import Store

from .exceptions import (
    AlreadyMember,
    CannotChangeOwnRole,
    InvitationEmailMismatch,
    InvitationInvalid,
    LastOwner,
    MemberNotFound,
    RoleTooHigh,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _expired(value: Any) -> bool:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= _utcnow()


# -- organization profile ----------------------------------------------


def create_organization(store: Store, user: CurrentUser, name: str) -> dict[str, Any]:
    slug = unique_org_slug(store, name)
    org_id = store.create_organization(slug, name.strip())
    store.add_membership(org_id, user.id, "owner")
    store.record_audit(
        AuditAction.ORG_CREATED.value,
        organization_id=org_id,
        actor_user_id=user.id,
        target_type="organization",
        target_id=org_id,
        metadata={"name": name.strip(), "slug": slug},
    )
    organization = store.organization(org_id)
    assert organization is not None
    return organization


def rename_organization(
    store: Store, ctx: OrgContext, name: str, *, ip: str | None
) -> dict[str, Any]:
    store.update_organization(ctx.organization_id, name)
    store.record_audit(
        AuditAction.ORG_UPDATED.value,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user.id,
        target_type="organization",
        target_id=ctx.organization_id,
        metadata={"name": name.strip()},
        ip=ip,
    )
    organization = store.organization(ctx.organization_id)
    assert organization is not None
    return organization


# -- invitations -----------------------------------------------------


def invite_member(
    store: Store,
    ctx: OrgContext,
    email: str,
    role: str,
    *,
    secret: str,
    ip: str | None,
) -> tuple[dict[str, Any], str]:
    if ROLE_RANK[role] > ROLE_RANK[ctx.role]:
        raise RoleTooHigh()
    email = email.strip().lower()

    existing_user = store.get_user_by_email(email)
    if existing_user and store.membership_role(ctx.organization_id, existing_user["id"]):
        raise AlreadyMember()

    # One live invitation per address; re-inviting replaces the old link.
    pending = store.pending_invitation_for(ctx.organization_id, email)
    if pending:
        store.delete_invitation(ctx.organization_id, pending["id"])

    raw = new_raw_token()
    invitation_id = store.create_invitation(
        ctx.organization_id,
        email,
        role,
        hash_token(raw, secret),
        ctx.user.id,
        _utcnow() + INVITATION_TTL,
    )
    store.record_audit(
        AuditAction.MEMBER_INVITED.value,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user.id,
        target_type="invitation",
        target_id=invitation_id,
        metadata={"email": email, "role": role},
        ip=ip,
    )
    invitation = store.get_invitation(ctx.organization_id, invitation_id)
    assert invitation is not None
    return invitation, raw


def revoke_invitation(
    store: Store, ctx: OrgContext, invitation_id: str, *, ip: str | None
) -> None:
    invitation = store.get_invitation(ctx.organization_id, invitation_id)
    if invitation is None or invitation["accepted_at"] is not None:
        raise InvitationInvalid("That invitation no longer exists.")
    store.delete_invitation(ctx.organization_id, invitation_id)
    store.record_audit(
        AuditAction.MEMBER_INVITE_REVOKED.value,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user.id,
        target_type="invitation",
        target_id=invitation_id,
        metadata={"email": invitation["email"]},
        ip=ip,
    )


def preview_invitation(store: Store, raw_token: str, *, secret: str) -> dict[str, Any]:
    invitation = store.invitation_by_token_hash(hash_token(raw_token, secret))
    if invitation is None or invitation["accepted_at"] is not None:
        raise InvitationInvalid()
    return {
        "organization_name": invitation["organization_name"],
        "email": invitation["email"],
        "role": invitation["role"],
        "expired": _expired(invitation["expires_at"]),
    }


def accept_invitation(
    store: Store, user: CurrentUser, raw_token: str, *, secret: str, ip: str | None
) -> str:
    invitation = store.invitation_by_token_hash(hash_token(raw_token, secret))
    if invitation is None or invitation["accepted_at"] is not None:
        raise InvitationInvalid()
    if _expired(invitation["expires_at"]):
        raise InvitationInvalid()
    if user.email != invitation["email"]:
        raise InvitationEmailMismatch()

    organization_id = invitation["organization_id"]
    invited_role = invitation["role"]
    current_role = store.membership_role(organization_id, user.id)
    if current_role is None:
        store.add_membership(organization_id, user.id, invited_role)
        effective_role = invited_role
    else:
        # Never downgrade someone who already has more access.
        effective_role = (
            invited_role
            if ROLE_RANK[invited_role] > ROLE_RANK[current_role]
            else current_role
        )
        if effective_role != current_role:
            store.set_membership_role(organization_id, user.id, effective_role)

    store.mark_invitation_accepted(invitation["id"])
    store.record_audit(
        AuditAction.MEMBER_JOINED.value,
        organization_id=organization_id,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        metadata={"role": effective_role, "invitation_id": invitation["id"]},
        ip=ip,
    )
    return organization_id


# -- member management ---------------------------------------------


def change_member_role(
    store: Store, ctx: OrgContext, target_user_id: str, new_role: str, *, ip: str | None
) -> None:
    if target_user_id == ctx.user.id:
        raise CannotChangeOwnRole()
    current = store.membership_role(ctx.organization_id, target_user_id)
    if current is None:
        raise MemberNotFound()
    if current == new_role:
        return
    if current == "owner" and store.count_role(ctx.organization_id, "owner") <= 1:
        raise LastOwner("demote")
    store.set_membership_role(ctx.organization_id, target_user_id, new_role)
    store.record_audit(
        AuditAction.MEMBER_ROLE_CHANGED.value,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user.id,
        target_type="user",
        target_id=target_user_id,
        metadata={"from": current, "to": new_role},
        ip=ip,
    )


def remove_member(
    store: Store, ctx: OrgContext, target_user_id: str, *, ip: str | None
) -> None:
    is_self = target_user_id == ctx.user.id
    if not is_self and ROLE_RANK[ctx.role] < ROLE_RANK["owner"]:
        raise Forbidden("Only an owner can remove another member.")
    current = store.membership_role(ctx.organization_id, target_user_id)
    if current is None:
        raise MemberNotFound()
    if current == "owner" and store.count_role(ctx.organization_id, "owner") <= 1:
        raise LastOwner("remove")
    store.remove_membership(ctx.organization_id, target_user_id)
    store.record_audit(
        AuditAction.MEMBER_REMOVED.value,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user.id,
        target_type="user",
        target_id=target_user_id,
        metadata={"role": current, "self": is_self},
        ip=ip,
    )
