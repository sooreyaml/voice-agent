from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import (
    OrgContext,
    OrgMemberDep,
    StoreDep,
    require_org_role,
)
from app.pagination import InvalidCursor, decode_cursor, encode_cursor

from . import service
from .constants import STATUS_DEAD, STATUS_FAILED, STATUS_PENDING, STATUS_SUCCEEDED
from .exceptions import WebhookDeliveryNotFound, WebhookEndpointNotFound
from .schemas import (
    CreateEndpointRequest,
    DeliveryDetailResponse,
    DeliveryPage,
    DeliveryResponse,
    EndpointResponse,
    EndpointWithSecretResponse,
    PageInfo,
    SecretResponse,
    UpdateEndpointRequest,
)

router = APIRouter(tags=["webhooks"])

OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]

DEFAULT_PAGE = 25
MAX_PAGE = 100
_DELIVERY_STATUSES = {
    STATUS_PENDING,
    "delivering",
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_DEAD,
}


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _events(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _endpoint_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "url": str(row["url"]),
        "description": row.get("description"),
        "event_types": _events(row.get("event_types")),
        "active": bool(row["active"]),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _delivery_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "webhook_event_id": str(row["webhook_event_id"]),
        "webhook_endpoint_id": str(row["webhook_endpoint_id"]),
        "event_type": str(row["event_type"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "next_attempt_at": row.get("next_attempt_at"),
        "last_attempt_at": row.get("last_attempt_at"),
        "last_status_code": row.get("last_status_code"),
        "last_error": row.get("last_error"),
        "created_at": row.get("created_at"),
    }


def _require_endpoint(store: StoreDep, org_id: str, endpoint_id: str) -> dict[str, Any]:
    row = store.webhook_endpoint(org_id, endpoint_id)
    if row is None:
        raise WebhookEndpointNotFound()
    return row


# -- endpoint CRUD ------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/webhook-endpoints",
    response_model=list[EndpointResponse],
    summary="List an organization's webhook endpoints",
)
def list_endpoints(context: OrgMemberDep, store: StoreDep) -> list[dict[str, Any]]:
    return [
        _endpoint_payload(row)
        for row in store.list_webhook_endpoints(context.organization_id)
    ]


@router.post(
    "/organizations/{organization_id}/webhook-endpoints",
    response_model=EndpointWithSecretResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint (admin or owner); returns the secret once",
)
def create_endpoint(
    body: CreateEndpointRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    secret = service.new_secret()
    endpoint_id = store.create_webhook_endpoint(
        context.organization_id,
        str(body.url),
        secret,
        body.description,
        json.dumps(body.event_types) if body.event_types else None,
        body.active,
    )
    store.record_audit(
        AuditAction.WEBHOOK_ENDPOINT_CREATED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id,
        target_type="webhook_endpoint",
        target_id=endpoint_id,
        metadata={"url": str(body.url), "event_types": body.event_types},
        ip=_ip(request),
    )
    row = store.webhook_endpoint(context.organization_id, endpoint_id)
    assert row is not None
    return {**_endpoint_payload(row), "secret": secret}


@router.get(
    "/organizations/{organization_id}/webhook-endpoints/{endpoint_id}",
    response_model=EndpointResponse,
    summary="One webhook endpoint",
    responses={404: {"description": "Endpoint not found"}},
)
def get_endpoint(
    endpoint_id: str, context: OrgMemberDep, store: StoreDep
) -> dict[str, Any]:
    return _endpoint_payload(
        _require_endpoint(store, context.organization_id, endpoint_id)
    )


@router.patch(
    "/organizations/{organization_id}/webhook-endpoints/{endpoint_id}",
    response_model=EndpointResponse,
    summary="Update a webhook endpoint (admin or owner)",
    responses={404: {"description": "Endpoint not found"}},
)
def update_endpoint(
    endpoint_id: str,
    body: UpdateEndpointRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    _require_endpoint(store, context.organization_id, endpoint_id)
    sent = body.model_fields_set
    fields: dict[str, Any] = {}
    if "url" in sent and body.url is not None:
        fields["url"] = str(body.url)
    if "description" in sent:
        fields["description"] = body.description
    if "event_types" in sent:
        fields["event_types"] = (
            json.dumps(body.event_types) if body.event_types else None
        )
    if "active" in sent and body.active is not None:
        fields["active"] = 1 if body.active else 0
    store.update_webhook_endpoint(context.organization_id, endpoint_id, fields)
    store.record_audit(
        AuditAction.WEBHOOK_ENDPOINT_UPDATED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id,
        target_type="webhook_endpoint",
        target_id=endpoint_id,
        metadata={"changed": sorted(fields)},
        ip=_ip(request),
    )
    row = store.webhook_endpoint(context.organization_id, endpoint_id)
    assert row is not None
    return _endpoint_payload(row)


@router.delete(
    "/organizations/{organization_id}/webhook-endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook endpoint (admin or owner)",
)
def delete_endpoint(
    endpoint_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> Response:
    _require_endpoint(store, context.organization_id, endpoint_id)
    store.delete_webhook_endpoint(context.organization_id, endpoint_id)
    store.record_audit(
        AuditAction.WEBHOOK_ENDPOINT_DELETED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id,
        target_type="webhook_endpoint",
        target_id=endpoint_id,
        ip=_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/organizations/{organization_id}/webhook-endpoints/{endpoint_id}/rotate-secret",
    response_model=SecretResponse,
    summary="Rotate the signing secret (admin or owner); returns the new secret once",
)
def rotate_secret(
    endpoint_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, str]:
    _require_endpoint(store, context.organization_id, endpoint_id)
    secret = service.new_secret()
    store.rotate_webhook_secret(context.organization_id, endpoint_id, secret)
    store.record_audit(
        AuditAction.WEBHOOK_SECRET_ROTATED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id,
        target_type="webhook_endpoint",
        target_id=endpoint_id,
        ip=_ip(request),
    )
    return {"secret": secret}


# -- delivery history + replay ---------------------------------------


@router.get(
    "/organizations/{organization_id}/webhook-endpoints/{endpoint_id}/deliveries",
    response_model=DeliveryPage,
    summary="Delivery history for one endpoint",
)
def list_deliveries(
    endpoint_id: str,
    context: OrgMemberDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    _require_endpoint(store, context.organization_id, endpoint_id)
    if status_filter is not None and status_filter not in _DELIVERY_STATUSES:
        status_filter = None
    try:
        before_id = decode_cursor(cursor)
    except InvalidCursor:
        before_id = None
    rows = store.webhook_deliveries_page(
        context.organization_id,
        endpoint_id,
        limit + 1,
        status_filter,
        before_id,
    )
    has_more = len(rows) > limit
    items = [_delivery_payload(r) for r in rows[:limit]]
    next_cursor = (
        encode_cursor(str(items[-1]["id"])) if has_more and items else None
    )
    return {"items": items, "page": PageInfo(next_cursor=next_cursor, has_more=has_more)}


@router.get(
    "/organizations/{organization_id}/webhook-deliveries/{delivery_id}",
    response_model=DeliveryDetailResponse,
    summary="One delivery with its full attempt history",
    responses={404: {"description": "Delivery not found"}},
)
def get_delivery(
    delivery_id: str, context: OrgMemberDep, store: StoreDep
) -> dict[str, Any]:
    row = store.webhook_delivery(context.organization_id, delivery_id)
    if row is None:
        raise WebhookDeliveryNotFound()
    try:
        payload = json.loads(row["event_payload"])
    except (TypeError, ValueError):
        payload = row.get("event_payload")
    return {
        **_delivery_payload(row),
        "payload": payload,
        "response_snippet": row.get("response_snippet"),
        "history": row.get("history", []),
    }


@router.post(
    "/organizations/{organization_id}/webhook-deliveries/{delivery_id}/retry",
    response_model=DeliveryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-queue a failed or dead-lettered delivery (admin or owner)",
    responses={404: {"description": "Delivery not found or not replayable"}},
)
def retry_delivery(
    delivery_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    if not store.reset_webhook_delivery(context.organization_id, delivery_id):
        raise WebhookDeliveryNotFound()
    store.record_audit(
        AuditAction.WEBHOOK_DELIVERY_REPLAYED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id,
        target_type="webhook_delivery",
        target_id=delivery_id,
        ip=_ip(request),
    )
    row = store.webhook_delivery(context.organization_id, delivery_id)
    assert row is not None
    return _delivery_payload(row)
