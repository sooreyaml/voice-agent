"""``require_org_access`` — one dependency that accepts either a session cookie
or a scoped bearer API key, and yields a uniform :class:`AccessContext`.

Cookie principals implicitly hold every scope; their membership role still gates
mutations through the existing ``require_org_role`` where needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.domains.auth.dependencies import (
    CurrentUser,
    SettingsDep,
    StoreDep,
    current_user,
    require_org_member,
)
from app.domains.auth.exceptions import OrganizationNotFound

from . import service
from .constants import ALL_SCOPES
from .exceptions import InvalidApiKey, MissingScope


@dataclass(frozen=True)
class AccessContext:
    organization_id: str
    principal: str  # "user:<id>" or "api_key:<id>"
    scopes: frozenset[str]
    user: CurrentUser | None = None  # None for API-key principals
    role: str | None = None  # membership role for cookie principals

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return None


def require_org_access(*required_scopes: str, min_role: str = "viewer"):
    """Build a dependency that authorises access to
    ``/organizations/{organization_id}/...`` for the given scopes.

    Bearer principals are checked against ``required_scopes``; cookie principals
    hold every scope but must meet ``min_role`` (default: any member).
    """

    def _dep(
        organization_id: str,
        request: Request,
        store: StoreDep,
        settings: SettingsDep,
    ) -> AccessContext:
        token = _bearer(request)
        if token is not None:
            key = service.authenticate(store, settings, token)
            if key is None:
                raise InvalidApiKey()
            if str(key["organization_id"]) != organization_id:
                # Same shape as a non-member cookie user: don't confirm the org.
                raise OrganizationNotFound()
            missing = sorted(set(required_scopes) - set(key["scope_set"]))
            if missing:
                raise MissingScope(missing)
            retry_after = request.app.state.runtime_state.check_api_key_rate_limit(
                str(key["id"]), settings.api_key_rate_limit_per_minute
            )
            if retry_after is not None:
                from .exceptions import ApiKeyRateLimited

                raise ApiKeyRateLimited(retry_after=retry_after)
            return AccessContext(
                organization_id=organization_id,
                principal=f"api_key:{key['id']}",
                scopes=frozenset(key["scope_set"]),
            )

        user = current_user(request, store, settings)
        member = require_org_member(organization_id, user, store)
        member.require_role(min_role)
        return AccessContext(
            organization_id=organization_id,
            principal=f"user:{user.id}",
            scopes=ALL_SCOPES,
            user=user,
            role=member.role,
        )

    return _dep


# Common pre-built dependencies.
CallsReadDep = Annotated[AccessContext, Depends(require_org_access("calls:read"))]
LeadsReadDep = Annotated[AccessContext, Depends(require_org_access("leads:read"))]
LeadsWriteDep = Annotated[
    AccessContext, Depends(require_org_access("leads:write", min_role="member"))
]
