from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.origins import origin_from_headers
from app.settings import Settings
from app.store import Store

from .constants import ROLE_RANK, SESSION_COOKIE
from .exceptions import Forbidden, NotAuthenticated, OrganizationNotFound
from .service import hash_token


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def request_origin(request: Request) -> str | None:
    """The frontend that issued this request, for choosing which base URL a
    link in an email / redirect should point back at."""
    return origin_from_headers(
        request.headers.get("origin"), request.headers.get("referer")
    )


StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    email_verified: bool
    is_platform_admin: bool
    session_id: str


@dataclass(frozen=True)
class OrgContext:
    organization_id: str
    role: str
    user: CurrentUser

    def require_role(self, minimum: str) -> None:
        if ROLE_RANK[self.role] < ROLE_RANK[minimum]:
            raise Forbidden(f"This action needs the {minimum} role.")


def current_user(
    request: Request, store: StoreDep, settings: SettingsDep
) -> CurrentUser:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise NotAuthenticated()
    row = store.active_session(hash_token(raw, settings.auth_session_secret))
    if row is None:
        raise NotAuthenticated("Your session has expired. Sign in again.")
    user = CurrentUser(
        id=str(row["user_id"]),
        email=str(row["email"]),
        email_verified=row["email_verified_at"] is not None,
        is_platform_admin=bool(row["is_platform_admin"]),
        session_id=str(row["session_id"]),
    )
    request.state.user = user
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


def require_org_member(
    organization_id: str, user: CurrentUserDep, store: StoreDep
) -> OrgContext:
    if store.organization(organization_id) is None:
        raise OrganizationNotFound()
    role = store.membership_role(organization_id, user.id)
    if role is None:
        # Same shape as a missing org so membership can't be probed.
        raise OrganizationNotFound()
    return OrgContext(organization_id=organization_id, role=role, user=user)


OrgMemberDep = Annotated[OrgContext, Depends(require_org_member)]


def require_org_role(minimum: str):
    def _dep(context: OrgMemberDep) -> OrgContext:
        context.require_role(minimum)
        return context

    return _dep


def require_platform_admin(user: CurrentUserDep) -> CurrentUser:
    if not user.is_platform_admin:
        raise Forbidden("Platform administrator access is required.")
    return user
