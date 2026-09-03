"""Self-service agent editing for an organization owner.

Wraps :class:`BusinessRepository`'s draft/publish machinery with the rules that
belong to the customer-facing API rather than the storage layer:

* the phone number is platform-managed — the owner never sets it, so an incoming
  configuration always has the organization's live number injected;
* a suspended or closed organization cannot change its agent;
* every save and publish is written to the audit log.
"""

from __future__ import annotations

import copy
from typing import Any

from app.domains.audit.models import AuditAction
from app.store import Store

from .constants import EDITABLE_LIFECYCLES
from .exceptions import AgentDraftNotFound, AgentLocked, AgentNotProvisioned
from .repository import BusinessRepository
from .schemas import PublishedBusinessConfiguration


def _overview(
    store: Store, organization_id: str
) -> tuple[BusinessRepository, dict[str, Any]]:
    repo = BusinessRepository(store)
    overview = repo.agent_overview(organization_id)
    if overview is None:
        raise AgentNotProvisioned()
    return repo, overview


def _lifecycle(store: Store, organization_id: str) -> str:
    org = store.organization(organization_id)
    return str((org or {}).get("lifecycle") or "active")


def get_state(store: Store, organization_id: str) -> dict[str, Any]:
    """Read model for the agent editor. Never raises for a missing profile —
    the dashboard renders a 'waiting for your number' panel from
    ``provisioned: false`` instead.
    """
    lifecycle = _lifecycle(store, organization_id)
    overview = BusinessRepository(store).agent_overview(organization_id)
    if overview is None:
        return {
            "provisioned": False,
            "lifecycle": lifecycle,
            "editable": False,
            "active_phone_numbers": [],
            "slug": None,
            "name": None,
            "published": None,
            "draft": None,
        }
    return {
        "provisioned": True,
        "lifecycle": lifecycle,
        "editable": lifecycle in EDITABLE_LIFECYCLES,
        "active_phone_numbers": overview["active_phone_numbers"],
        "slug": overview["slug"],
        "name": overview["name"],
        "published": overview["published"],
        "draft": overview["draft"],
    }


def get_draft(store: Store, organization_id: str) -> dict[str, Any]:
    _repo, overview = _overview(store, organization_id)
    if overview["draft"] is None:
        raise AgentDraftNotFound()
    return overview["draft"]


def save_draft(
    store: Store,
    organization_id: str,
    configuration: dict[str, Any],
    *,
    actor_user_id: str | None,
    ip: str | None,
) -> dict[str, Any]:
    repo, overview = _overview(store, organization_id)
    if _lifecycle(store, organization_id) not in EDITABLE_LIFECYCLES:
        raise AgentLocked()

    numbers = overview["active_phone_numbers"] or _published_numbers(overview)
    if not numbers:
        # No live number to pin the profile to (suspended mid-edit, or the
        # signup provisioning failed and has not been retried yet).
        raise AgentNotProvisioned()

    config = copy.deepcopy(configuration)
    config.setdefault("business", {})["phone_numbers"] = numbers
    # Validate here so a bad body is a clean 422 rather than surfacing from
    # deep inside the repository transaction.
    PublishedBusinessConfiguration.model_validate(config)

    from app.business import BusinessProfile

    draft = repo.save_draft(
        organization_id, BusinessProfile(raw=config, slug=overview["slug"])
    )
    store.record_audit(
        AuditAction.PROFILE_DRAFT_SAVED.value,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        target_type="agent_version",
        target_id=draft.version_id,
        metadata={"version": draft.version_number, "source": "self_service"},
        ip=ip,
    )
    overview = repo.agent_overview(organization_id)
    assert overview is not None and overview["draft"] is not None
    return overview["draft"]


def discard_draft(
    store: Store,
    organization_id: str,
    *,
    actor_user_id: str | None,
    ip: str | None,
) -> None:
    repo, overview = _overview(store, organization_id)
    if not repo.discard_draft(organization_id, overview["slug"]):
        raise AgentDraftNotFound()
    store.record_audit(
        AuditAction.PROFILE_DRAFT_DISCARDED.value,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        target_type="business_profile",
        target_id=overview["slug"],
        metadata={"source": "self_service"},
        ip=ip,
    )


def publish(
    store: Store,
    organization_id: str,
    *,
    actor_user_id: str | None,
    ip: str | None,
) -> dict[str, Any]:
    repo, overview = _overview(store, organization_id)
    if _lifecycle(store, organization_id) not in EDITABLE_LIFECYCLES:
        raise AgentLocked()
    if overview["draft"] is None:
        raise AgentDraftNotFound()

    published = repo.publish_draft(organization_id, overview["slug"])
    store.record_audit(
        AuditAction.PROFILE_PUBLISHED.value,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        target_type="agent_version",
        target_id=published.version_id,
        metadata={
            "version": published.version_number,
            "phone_numbers": published.phone_numbers,
            "source": "self_service",
        },
        ip=ip,
    )
    overview = repo.agent_overview(organization_id)
    assert overview is not None and overview["published"] is not None
    return overview["published"]


def _published_numbers(overview: dict[str, Any]) -> list[str]:
    published = overview.get("published")
    if not published:
        return []
    business = published.get("configuration", {}).get("business", {})
    return [str(n) for n in business.get("phone_numbers", [])]
