"""Self-service agent configuration for an organization owner.

One organization has exactly one agent (created and published at signup), so
these routes address it as ``.../agent`` with no profile id. Reads are open to
any member; changes need ``admin``. Editing is a draft/publish cycle: a saved
draft never affects live calls until it is published.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.domains.auth.dependencies import (
    OrgContext,
    OrgMemberDep,
    StoreDep,
    require_org_role,
)

from . import service
from .schemas import (
    AgentDraftRequest,
    AgentDraftResponse,
    AgentStateResponse,
)

router = APIRouter(tags=["agent"])

OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/organizations/{organization_id}/agent",
    response_model=AgentStateResponse,
    summary="The organization's live agent, its draft, and edit eligibility",
)
def get_agent(context: OrgMemberDep, store: StoreDep) -> dict[str, Any]:
    return service.get_state(store, context.organization_id)


@router.get(
    "/organizations/{organization_id}/agent/draft",
    response_model=AgentDraftResponse,
    summary="The unpublished draft with its rendered prompt preview",
    responses={
        404: {"description": "No draft exists"},
        409: {"description": "Organization has no agent yet"},
    },
)
def get_agent_draft(context: OrgMemberDep, store: StoreDep) -> dict[str, Any]:
    return service.get_draft(store, context.organization_id)


@router.put(
    "/organizations/{organization_id}/agent/draft",
    response_model=AgentDraftResponse,
    summary="Save a full replacement configuration as the draft (admin or owner)",
    responses={
        409: {"description": "Organization has no agent yet, or is suspended/closed"},
        422: {"description": "Configuration failed validation"},
    },
)
def put_agent_draft(
    body: AgentDraftRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.save_draft(
        store,
        context.organization_id,
        body.model_dump(mode="json"),
        actor_user_id=context.user.id,
        ip=_ip(request),
    )


@router.delete(
    "/organizations/{organization_id}/agent/draft",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard the draft, keeping the live agent unchanged (admin or owner)",
    responses={404: {"description": "No draft to discard"}},
)
def delete_agent_draft(
    context: OrgAdminDep, store: StoreDep, request: Request
) -> Response:
    service.discard_draft(
        store,
        context.organization_id,
        actor_user_id=context.user.id,
        ip=_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{organization_id}/agent/publish",
    response_model=AgentDraftResponse,
    summary="Publish the draft — the new configuration answers the next call",
    responses={
        404: {"description": "No draft to publish"},
        409: {"description": "Organization has no agent yet, or is suspended/closed"},
    },
)
def publish_agent(
    context: OrgAdminDep, store: StoreDep, request: Request
) -> dict[str, Any]:
    return service.publish(
        store,
        context.organization_id,
        actor_user_id=context.user.id,
        ip=_ip(request),
    )
