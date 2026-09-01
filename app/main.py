from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from openai import InvalidWebhookSignatureError, OpenAI

from .api import install_api
from .domains.billing.services import spend as spend_service
from .domains.businesses.repository import BusinessRepository
from .domains.integrations import service as integrations_service
from .domains.integrations.crypto import build_cipher
from .migrations import upgrade_database
from .realtime import RealtimeCalls
from .runtime_state import build_runtime_state
from .session import CallSession
from .settings import settings
from .store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logger = logging.getLogger("callagent")

# Sessions run detached from the request; keep references so they are not GC'd.
_live_sessions: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    required = ["openai_api_key", "openai_webhook_secret"]
    if settings.environment != "development":
        required.extend(
            ["auth_session_secret", "integration_encryption_key", "redis_url"]
        )
    settings.require(*required)
    app.state.settings = settings
    logger.info("applying database migrations")
    await run_in_threadpool(upgrade_database, settings.database_target)
    app.state.store = await run_in_threadpool(
        Store, settings.database_target, migrate=False
    )
    logger.info("call history stored in %s", app.state.store.dialect)
    app.state.runtime_state = await run_in_threadpool(build_runtime_state, settings)
    logger.info("shared runtime state stored in %s", app.state.runtime_state.backend)
    app.state.business_repository = BusinessRepository(app.state.store)
    app.state.credential_cipher = build_cipher(settings)
    app.state.calls = RealtimeCalls(settings.openai_api_key)
    app.state.openai = OpenAI(
        api_key=settings.openai_api_key, webhook_secret=settings.openai_webhook_secret
    )
    profiles = await run_in_threadpool(app.state.business_repository.list_published)
    if not profiles:
        logger.warning(
            "no published business profiles; seed an organization before routing calls"
        )
    else:
        logger.info(
            "serving %d business(es): %s",
            len(profiles),
            ", ".join(p.name for p in profiles),
        )
    logger.info(
        "point your SIP trunk at: %s", settings.sip_uri or "(set OPENAI_PROJECT_ID)"
    )
    try:
        yield
    finally:
        await app.state.calls.close()
        await run_in_threadpool(app.state.runtime_state.close)
        await run_in_threadpool(app.state.store.close)


# Interactive docs are a development/staging aid; hide the schema in production.
_docs = {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
if not settings.docs_enabled:
    _docs = {"docs_url": "/docs", "redoc_url": None, "openapi_url": None}

app = FastAPI(title="Call Agent", lifespan=lifespan, **_docs)
install_api(app)


def _sip_header(headers: list[Any], name: str) -> str:
    """Pull a value like 'sip:+14155550123@...' out of the SIP header list."""
    for header in headers or []:
        key = (
            header.get("name")
            if isinstance(header, dict)
            else getattr(header, "name", "")
        )
        if (key or "").lower() != name.lower():
            continue
        value = (
            header.get("value")
            if isinstance(header, dict)
            else getattr(header, "value", "")
        ) or ""
        if "sip:" in value:
            value = value.split("sip:", 1)[1].split("@", 1)[0]
        return value.strip("<> ")
    return ""


@app.post("/openai/webhook")
async def openai_webhook(request: Request) -> Response:
    body = await request.body()
    try:
        event = request.app.state.openai.webhooks.unwrap(body, dict(request.headers))
    except (InvalidWebhookSignatureError, ValueError) as exc:
        # ValueError covers requests missing the signature headers entirely,
        # which is what random internet traffic hitting this endpoint looks like.
        logger.warning("rejected unverified webhook: %s", exc)
        raise HTTPException(status_code=400, detail="invalid signature")

    if event.type != "realtime.call.incoming":
        logger.info("ignoring webhook event %s", event.type)
        return Response(status_code=200)

    delivery_id = request.headers.get("webhook-id", "")

    call_id = event.data.call_id
    sip_headers = getattr(event.data, "sip_headers", []) or []
    from_number = _sip_header(sip_headers, "From")
    to_number = _sip_header(sip_headers, "To")
    logger.info(
        "incoming call %s from %s to %s", call_id, from_number or "?", to_number or "?"
    )

    profile = await run_in_threadpool(
        request.app.state.business_repository.find_by_phone_number,
        to_number,
    )
    if profile is None:
        # Nothing sensible to say to this caller, so decline at the SIP level
        # rather than answering with the wrong business's greeting.
        await request.app.state.calls.reject(call_id, status_code=404)
        return Response(status_code=200)

    allowed = await run_in_threadpool(
        spend_service.call_is_allowed,
        request.app.state.store,
        profile.organization_id,
    )
    if not allowed:
        logger.warning("monthly spend limit blocked incoming call %s", call_id)
        await request.app.state.calls.reject(call_id, status_code=402)
        return Response(status_code=200)

    # Connected integrations unlock extra tools for this call. A broken or
    # unreadable integration must never stop us answering the phone.
    def _load_integrations() -> tuple[object | None, object | None]:
        store = request.app.state.store
        cipher = request.app.state.credential_cipher
        org_id = profile.organization_id
        return (
            integrations_service.load_calendar_provider(store, cipher, org_id),
            integrations_service.load_crm_provider(store, cipher, org_id),
        )

    try:
        calendar, crm = await run_in_threadpool(_load_integrations)
    except Exception:
        logger.exception("could not load integrations for %s", call_id)
        calendar, crm = None, None

    session = CallSession(
        organization_id=profile.organization_id,
        call_id=call_id,
        from_number=from_number,
        to_number=to_number,
        profile=profile,
        settings=settings,
        store=request.app.state.store,
        calls=request.app.state.calls,
        calendar=calendar,
        crm=crm,
        runtime_state=request.app.state.runtime_state,
    )

    # The carrier is waiting on this INVITE, so answer before doing anything else.
    if delivery_id:
        claimed = await run_in_threadpool(
            request.app.state.runtime_state.claim_webhook, delivery_id
        )
        if not claimed:
            logger.info("duplicate delivery %s ignored", delivery_id)
            return Response(status_code=200)
    try:
        await request.app.state.calls.accept(call_id, session.session_config())
    except Exception:
        logger.exception("could not accept call %s", call_id)
        if delivery_id:
            await run_in_threadpool(
                request.app.state.runtime_state.release_webhook, delivery_id
            )
        raise HTTPException(status_code=500, detail="accept failed")

    task = asyncio.create_task(session.run())
    _live_sessions.add(task)
    task.add_done_callback(_live_sessions.discard)
    if delivery_id:
        await run_in_threadpool(
            request.app.state.runtime_state.complete_webhook, delivery_id
        )
    return Response(status_code=200)


@app.get("/health", summary="Check call-agent service health", tags=["operations"])
def health(request: Request) -> dict[str, Any]:
    profiles = request.app.state.business_repository.list_published()
    return {
        "status": "ok",
        "businesses": [
            {
                "organization_id": profile.organization_id,
                "profile_id": profile.profile_id,
                "version_id": profile.version_id,
                "version": profile.version_number,
                "slug": profile.slug,
                "name": profile.name,
                "numbers": profile.phone_numbers,
            }
            for profile in profiles
        ],
        "storage": request.app.state.store.dialect,
        "model": settings.realtime_model,
        "active_calls": request.app.state.runtime_state.active_call_count(),
        "runtime_state": request.app.state.runtime_state.backend,
        "sip_uri": settings.sip_uri,
    }


# Tenant-scoped call history moved to the authenticated management API in
# app/api.py: GET /api/v1/organizations/{organization_id}/calls[/{call_id}].
