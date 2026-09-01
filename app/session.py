from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets
from fastapi.concurrency import run_in_threadpool
from websockets.asyncio.client import connect

from . import tools as tool_registry
from .business import BusinessProfile, render_instructions
from .domains.billing.usage import call_usage_events
from .domains.integrations.base import CalendarProvider, CrmProvider
from .pricing import cost_of_usage
from .realtime import RealtimeCalls, build_session_config, websocket_url
from .runtime_state import RuntimeState
from .settings import Settings
from .store import Store, transcript_as_text

logger = logging.getLogger(__name__)

# Rough speaking rate, used only to guess how much audio is still playing out
# to the caller before we are allowed to hang up on them.
CHARS_PER_SECOND = 14.0
MAX_DRAIN_SECONDS = 15.0
# Extra cushion for network and carrier jitter before we cut the line. Raise it
# if callers report the goodbye being clipped.
HANGUP_GRACE_SECONDS = 0.75

# /accept returns 200 while the realtime session is still being established, so
# the sideband socket can 404 for a moment afterwards. Keep retrying briefly:
# giving up on the first attempt drops the call while the caller hears ringing.
# A single attempt has been seen taking seconds to return its 404, so the total
# time is capped as well — the caller is listening to silence throughout.
WS_CONNECT_ATTEMPTS = 8
WS_RETRY_SECONDS = 0.4
WS_CONNECT_DEADLINE_SECONDS = 12.0
# Generous enough that a slow handshake reports its real status instead of being
# cut off as a timeout, which hides why the attach failed.
WS_OPEN_TIMEOUT_SECONDS = 8.0
# The sideband endpoint expects a browser-style handshake and answers 404 without
# this, which reads as "call not found" rather than "you are missing a header".
WS_ORIGIN = "https://api.openai.com"
RUNTIME_HEARTBEAT_SECONDS = 15


def _estimate_speech_seconds(text: str) -> float:
    return min(len(text) / CHARS_PER_SECOND, MAX_DRAIN_SECONDS)


class CallSession:
    """Owns one phone call from answer to hang-up.

    The audio flows directly between the caller and OpenAI. This class holds a
    second, text-only WebSocket to the same session so that tool calls and
    business logic stay on our server.
    """

    def __init__(
        self,
        *,
        organization_id: str,
        call_id: str,
        from_number: str,
        to_number: str,
        profile: BusinessProfile,
        settings: Settings,
        store: Store,
        calls: RealtimeCalls,
        calendar: CalendarProvider | None = None,
        crm: CrmProvider | None = None,
        runtime_state: RuntimeState | None = None,
        ws_url: str | None = None,
    ):
        self._ws_url = ws_url or websocket_url(call_id)
        self.organization_id = organization_id
        self.call_id = call_id
        self.from_number = from_number
        self.to_number = to_number
        self.profile = profile
        self.settings = settings
        self.store = store
        self.calls = calls
        self.calendar = calendar
        self.crm = crm
        self.runtime_state = runtime_state

        self.outcome: str = ""
        self.intent: str = ""
        self.model_cost: float = 0.0
        self.realtime_usage: dict[str, int] = {}
        self.summary_usage: dict[str, int] = {}

        self._ws: Any = None
        self._hangup_requested = False
        self._last_agent_text = ""
        self._last_agent_at = 0.0
        self._started_monotonic = 0.0

    # -- helpers ----------------------------------------------------------

    async def note(self, text: str) -> None:
        """Record something the system did, for the post-call record."""
        await run_in_threadpool(
            self.store.add_turn, self.organization_id, self.call_id, "tool", text
        )

    def request_hangup(self) -> None:
        self._hangup_requested = True

    async def _record(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        await run_in_threadpool(
            self.store.add_turn, self.organization_id, self.call_id, role, text
        )
        logger.info("[%s] %s: %s", self.call_id[-8:], role, text)
        if role == "agent":
            self._last_agent_text = text
            self._last_agent_at = time.monotonic()

    async def _send(self, event: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(event))

    # -- lifecycle --------------------------------------------------------

    def session_config(self) -> dict[str, Any]:
        return build_session_config(
            model=self.settings.realtime_model,
            instructions=render_instructions(self.profile),
            voice=self.profile.voice or self.settings.realtime_voice,
            tools=tool_registry.tool_schemas(
                self.profile,
                calendar=self.calendar is not None,
                crm=self.crm is not None,
            ),
            transcribe_caller=self.settings.transcribe_caller,
            transcribe_model=self.settings.transcribe_model,
        )

    async def _connect(self):
        """Open the sideband socket, tolerating a session that is not ready yet."""
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        give_up_at = time.monotonic() + WS_CONNECT_DEADLINE_SECONDS
        last: Exception | None = None
        for attempt in range(1, WS_CONNECT_ATTEMPTS + 1):
            try:
                ws = await connect(
                    self._ws_url,
                    additional_headers=headers,
                    origin=WS_ORIGIN,
                    max_size=None,
                    open_timeout=WS_OPEN_TIMEOUT_SECONDS,
                )
            except websockets.exceptions.InvalidStatus as exc:
                # 404 means the call is not attached yet; anything else is real.
                if exc.response.status_code != 404:
                    raise
                last = exc
            except TimeoutError as exc:
                # Worth distinguishing: a hung handshake is a different problem
                # from the API actively saying the call is not there.
                logger.warning(
                    "call %s handshake timed out after %.0fs (attempt %d)",
                    self.call_id,
                    WS_OPEN_TIMEOUT_SECONDS,
                    attempt,
                )
                last = exc
            else:
                logger.info("call %s attached on attempt %d", self.call_id, attempt)
                return ws
            if attempt == WS_CONNECT_ATTEMPTS:
                break
            if time.monotonic() + WS_RETRY_SECONDS >= give_up_at:
                logger.warning(
                    "call %s gave up attaching after %d attempts", self.call_id, attempt
                )
                break
            logger.info(
                "call %s not attached yet (attempt %d), retrying", self.call_id, attempt
            )
            await asyncio.sleep(WS_RETRY_SECONDS)
        raise RuntimeError(
            f"realtime session for {self.call_id} never became available"
        ) from last

    async def run(self) -> None:
        """Attach to the accepted call and pump events until it ends."""
        self._started_monotonic = time.monotonic()
        await run_in_threadpool(
            self.store.start_call,
            self.organization_id,
            self.call_id,
            self.profile.name,
            self.from_number,
            self.to_number,
            self.profile.version_id,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        if self.runtime_state is not None:
            try:
                await run_in_threadpool(
                    self.runtime_state.register_call,
                    self.call_id,
                    self.organization_id,
                )
                heartbeat_task = asyncio.create_task(self._heartbeat_runtime())
            except Exception as exc:  # noqa: BLE001 - shared state cannot drop a call
                logger.warning(
                    "active-call registration failed for %s: %s", self.call_id, exc
                )
        try:
            async with await self._connect() as ws:
                self._ws = ws
                await self._greet()
                async for raw in ws:
                    try:
                        await self._handle(json.loads(raw))
                    except Exception:
                        logger.exception("error handling event on %s", self.call_id)
                    if self._hangup_requested:
                        await self._drain_and_hangup()
                        break
        except websockets.exceptions.ConnectionClosed:
            logger.info("call %s closed", self.call_id)
        except Exception:
            logger.exception("session failed for %s", self.call_id)
            self.outcome = self.outcome or "error"
        finally:
            self._ws = None
            try:
                await self._finalize()
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat_task
                if self.runtime_state is not None:
                    try:
                        await run_in_threadpool(
                            self.runtime_state.finish_call, self.call_id
                        )
                    except Exception as exc:  # noqa: BLE001 - teardown is best effort
                        logger.warning(
                            "active-call cleanup failed for %s: %s", self.call_id, exc
                        )

    async def _heartbeat_runtime(self) -> None:
        while True:
            await asyncio.sleep(RUNTIME_HEARTBEAT_SECONDS)
            if (
                self.runtime_state is None
            ):  # pragma: no cover - task only starts with it
                return
            try:
                await run_in_threadpool(
                    self.runtime_state.heartbeat_call,
                    self.call_id,
                    self.organization_id,
                )
            except Exception as exc:  # noqa: BLE001 - heartbeat must not drop a call
                logger.warning(
                    "active-call heartbeat failed for %s: %s", self.call_id, exc
                )

    async def _greet(self) -> None:
        """Speak a fixed opening line instead of letting the model improvise."""
        await self._send(
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Say exactly this and nothing more, then stop and listen: "
                        f'"{self.profile.greeting}"'
                    )
                },
            }
        )

    # -- event handling ---------------------------------------------------

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")

        if kind == "conversation.item.input_audio_transcription.completed":
            await self._record("caller", event.get("transcript", ""))

        elif kind == "response.output_audio_transcript.done":
            await self._record("agent", event.get("transcript", ""))

        elif kind == "response.done":
            await self._handle_response_done(event.get("response") or {})

        elif kind == "error":
            logger.error("realtime error on %s: %s", self.call_id, event.get("error"))

    async def _handle_response_done(self, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        if usage:
            self.model_cost += cost_of_usage(self.settings.realtime_model, usage)
            incoming = usage.get("input_token_details") or {}
            outgoing = usage.get("output_token_details") or {}
            cached = incoming.get("cached_tokens_details") or {}
            additions = {
                "audio_input": int(incoming.get("audio_tokens") or 0),
                "text_input": int(incoming.get("text_tokens") or 0),
                "cached_audio_input": int(cached.get("audio_tokens") or 0),
                "cached_text_input": int(cached.get("text_tokens") or 0),
                "audio_output": int(outgoing.get("audio_tokens") or 0),
                "text_output": int(outgoing.get("text_tokens") or 0),
            }
            for kind, quantity in additions.items():
                self.realtime_usage[kind] = self.realtime_usage.get(kind, 0) + quantity

        calls = [
            item
            for item in (response.get("output") or [])
            if item.get("type") == "function_call"
        ]
        if not calls:
            return

        for item in calls:
            await self._run_tool(item)

        # Hand control back to the model so it can speak the result, unless a
        # tool asked us to end the call.
        if not self._hangup_requested and self.outcome != "transferred":
            await self._send({"type": "response.create"})

    async def _run_tool(self, item: dict[str, Any]) -> None:
        name = item.get("name", "")
        call_id = item.get("call_id")
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}

        logger.info("[%s] tool %s %s", self.call_id[-8:], name, args)
        try:
            result = await tool_registry.dispatch(self, name, args)
        except Exception:
            logger.exception("tool %s failed", name)
            result = {
                "status": "error",
                "instruction": "That did not work. Apologise briefly and continue.",
            }

        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            }
        )

    async def _drain_and_hangup(self) -> None:
        """Wait for the goodbye to finish playing, then cut the line."""
        remaining = _estimate_speech_seconds(self._last_agent_text)
        if self._last_agent_at:
            remaining -= time.monotonic() - self._last_agent_at
        await asyncio.sleep(max(remaining, 0.0) + HANGUP_GRACE_SECONDS)
        try:
            await self.calls.hangup(self.call_id)
        except Exception as exc:  # noqa: BLE001 - hangup is best-effort teardown
            logger.warning("hangup failed for %s: %s", self.call_id, exc)

    # -- after the call ---------------------------------------------------

    async def _finalize(self) -> None:
        turns = await run_in_threadpool(
            self.store.transcript, self.organization_id, self.call_id
        )
        summary = ""
        if turns:
            summary = await self._write_summary(turns)
        outcome = self.outcome or ("completed" if turns else "no_conversation")
        duration_seconds = max(math.ceil(time.monotonic() - self._started_monotonic), 1)
        occurred_at = datetime.now(UTC).replace(microsecond=0)
        usage_events = call_usage_events(
            organization_id=self.organization_id,
            call_id=self.call_id,
            occurred_at=occurred_at,
            duration_seconds=duration_seconds,
            realtime_model=self.settings.realtime_model,
            realtime_usage=self.realtime_usage,
            realtime_cost_micros=round(self.model_cost * 1_000_000),
            transcription_enabled=self.settings.transcribe_caller,
            transcription_model=self.settings.transcribe_model,
            summary_model=self.settings.summary_model,
            summary_usage=self.summary_usage,
            transferred=outcome == "transferred",
        )
        await run_in_threadpool(
            self.store.finish_call,
            self.organization_id,
            self.call_id,
            outcome,
            summary,
            self.model_cost,
            usage_events,
        )
        logger.info(
            "call %s ended (%s), model cost $%.4f",
            self.call_id,
            outcome,
            self.model_cost,
        )
        await self._emit_webhook_events(outcome, summary)
        await self._enqueue_crm_sync()
        await self._notify(outcome, summary)

    def _event_payload(self, outcome: str, summary: str, transcript: list) -> dict:
        try:
            parsed: Any = json.loads(summary) if summary else {}
        except json.JSONDecodeError:
            parsed = {"summary": summary}
        return {
            "call_id": self.call_id,
            "business": self.profile.name,
            "business_slug": self.profile.slug,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "outcome": outcome,
            "intent": self.intent,
            "model_cost_usd": round(self.model_cost, 6),
            "summary": parsed,
            "transcript": transcript,
        }

    async def _emit_webhook_events(self, outcome: str, summary: str) -> None:
        """Queue call.completed (and call.transferred) for signed delivery to any
        endpoints the organization has registered. Never blocks call teardown.
        """
        from .domains.webhooks import service as webhook_service
        from .domains.webhooks.constants import (
            EVENT_CALL_COMPLETED,
            EVENT_CALL_TRANSFERRED,
        )

        transcript = await run_in_threadpool(
            self.store.transcript, self.organization_id, self.call_id
        )
        data = self._event_payload(outcome, summary, transcript)
        event_types = [EVENT_CALL_COMPLETED]
        if outcome == "transferred":
            event_types.append(EVENT_CALL_TRANSFERRED)
        for event_type in event_types:
            try:
                await run_in_threadpool(
                    webhook_service.emit_event,
                    self.store,
                    organization_id=self.organization_id,
                    event_type=event_type,
                    dedupe_key=self.call_id,
                    data=data,
                    max_attempts=self.settings.webhook_max_attempts,
                )
            except Exception as exc:  # noqa: BLE001 - queueing must not block teardown
                logger.warning(
                    "webhook emit %s failed for %s: %s",
                    event_type,
                    self.call_id,
                    exc,
                )

    async def _enqueue_crm_sync(self) -> None:
        """Queue a post-call push to the org's CRM, if one is connected. The
        worker does the slow provider calls; a queue problem must not block
        teardown.
        """
        from .domains.integrations import crm_sync

        try:
            await run_in_threadpool(
                crm_sync.enqueue_call_sync,
                self.store,
                self.organization_id,
                self.call_id,
            )
        except Exception as exc:  # noqa: BLE001 - queueing must not block teardown
            logger.warning("crm sync enqueue failed for %s: %s", self.call_id, exc)

    async def _write_summary(self, turns: list[dict[str, str]]) -> str:
        """One cheap text call to turn the transcript into something readable."""
        from openai import AsyncOpenAI

        prompt = (
            f"Below is a transcript of a phone call to {self.profile.name}. Write a "
            "short handover note for the staff who will follow up.\n\n"
            "Reply with only a JSON object with these keys: "
            '"summary" (two sentences max), "caller_wants" (one short phrase), '
            '"action_required" (what a human must now do, or "none"), '
            '"sentiment" (one of positive, neutral, negative), '
            '"unanswered" (anything the agent could not answer, or "none").\n\n'
            f"Transcript:\n{transcript_as_text(turns)}"
        )
        try:
            client = AsyncOpenAI(api_key=self.settings.openai_api_key)
            response = await client.responses.create(
                model=self.settings.summary_model, input=prompt
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.summary_usage = {
                    "input": int(getattr(usage, "input_tokens", 0) or 0),
                    "output": int(getattr(usage, "output_tokens", 0) or 0),
                }
            return (response.output_text or "").strip()
        except Exception as exc:  # noqa: BLE001 - summaries are optional
            logger.warning("summary failed for %s: %s", self.call_id, exc)
            return ""

    async def _notify(self, outcome: str, summary: str) -> None:
        from .domains.calls.notifications import deliver_call_summary

        # A business can name its own webhook; otherwise fall back to the
        # server-wide one. Email is tenant-specific and must be opted into on
        # the published business profile.
        url = self.profile.notify_webhook or self.settings.notify_webhook_url
        recipient = self.profile.notify_email
        if not url and not recipient:
            return
        try:
            parsed: Any = json.loads(summary) if summary else {}
        except json.JSONDecodeError:
            parsed = {"summary": summary}

        if recipient:
            try:
                await run_in_threadpool(
                    deliver_call_summary,
                    recipient=recipient,
                    business_name=self.profile.name,
                    call_id=self.call_id,
                    from_number=self.from_number,
                    outcome=outcome,
                    summary=parsed,
                    resend_api_key=self.settings.resend_api_key,
                    resend_from_email=self.settings.resend_from_email,
                )
            except Exception as exc:  # noqa: BLE001 - notification is optional
                logger.warning("post-call email failed for %s: %s", self.call_id, exc)

        if not url:
            return

        transcript = await run_in_threadpool(
            self.store.transcript, self.organization_id, self.call_id
        )
        payload = {
            "call_id": self.call_id,
            "organization_id": self.organization_id,
            "business": self.profile.name,
            "business_slug": self.profile.slug,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "outcome": outcome,
            "intent": self.intent,
            "model_cost_usd": round(self.model_cost, 6),
            "summary": parsed,
            "transcript": transcript,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 - legacy notification is optional
            logger.warning("notify webhook failed for %s: %s", self.call_id, exc)
