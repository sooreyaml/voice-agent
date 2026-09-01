"""Runs a complete call against a fake Realtime server."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import session as session_module
from app import tools as tool_registry
from app.business import BusinessProfile
from app.domains.businesses.repository import BusinessRepository
from app.domains.integrations.base import Booking, CalendarProviderError, TimeSlot
from app.session import CallSession
from app.settings import load_settings
from app.store import Store

from .fake_realtime import AGENT_ANSWERS, CALLER_ASKS, GREETING, FakeRealtime

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"


class StubCalls:
    """Stands in for the /v1/realtime/calls REST endpoints."""

    def __init__(self) -> None:
        self.hangups: list[str] = []
        self.refers: list[tuple[str, str]] = []

    async def hangup(self, call_id: str) -> None:
        self.hangups.append(call_id)

    async def refer(self, call_id: str, target_uri: str) -> None:
        self.refers.append((call_id, target_uri))


@pytest.fixture
def profile() -> BusinessProfile:
    loaded = BusinessProfile.load(BUSINESSES / "harborview-dental.yaml")
    # The transfer tests need a destination, but the sample config exists to be
    # rewritten per installation, so pin one here instead of relying on it.
    loaded.raw.setdefault("contact", {})["transfer_to"] = "+441616960976"
    return loaded


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "calls.sqlite3")


@pytest.fixture
def settings(tmp_path: Path):
    return replace(
        load_settings(),
        openai_api_key="sk-test",
        businesses_dir=BUSINESSES,
        database_path=tmp_path / "calls.sqlite3",
        notify_webhook_url="",
    )


@pytest.fixture(autouse=True)
def fast_hangup(monkeypatch: pytest.MonkeyPatch):
    """Skip the real-time wait for the goodbye audio to play out."""
    monkeypatch.setattr(session_module, "CHARS_PER_SECOND", 10_000.0)
    monkeypatch.setattr(session_module, "HANGUP_GRACE_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def no_summary_call(monkeypatch: pytest.MonkeyPatch):
    """The summary is a separate paid API call; stub it out."""

    async def fake_summary(self, turns):
        return json.dumps({"summary": "Caller asked about cleaning prices."})

    monkeypatch.setattr(CallSession, "_write_summary", fake_summary)


async def run_call(
    profile,
    settings,
    store,
    scenario: str = "default",
    calls: StubCalls | None = None,
    reject_first: int = 0,
    reject_status: int = 404,
    calendar=None,
    crm=None,
) -> tuple[CallSession, StubCalls, FakeRealtime]:
    calls = calls or StubCalls()
    profile = BusinessRepository(store).publish(profile)
    async with FakeRealtime(scenario, reject_first, reject_status) as fake:
        sess = CallSession(
            organization_id=profile.organization_id,
            call_id="rtc_test_1",
            from_number="+16175550188",
            to_number="+16175550142",
            profile=profile,
            settings=settings,
            store=store,
            calls=calls,  # type: ignore[arg-type]
            calendar=calendar,
            crm=crm,
            ws_url=fake.url,
        )
        await sess.run()
        return sess, calls, fake


class FailingTransfer(StubCalls):
    async def refer(self, call_id: str, target_uri: str) -> None:
        raise RuntimeError("carrier rejected the REFER")


@pytest.mark.asyncio
async def test_greeting_is_scripted_not_improvised(profile, settings, store):
    _, _, fake = await run_call(profile, settings, store)
    opening = fake.recorder.of_type("response.create")[0]
    instructions = opening["response"]["instructions"]
    assert profile.greeting in instructions
    assert "Say exactly this" in instructions


@pytest.mark.asyncio
async def test_transcript_captures_both_sides(profile, settings, store):
    sess, _, _ = await run_call(profile, settings, store)
    turns = store.transcript(sess.organization_id, sess.call_id)
    said = {(t["role"], t["text"]) for t in turns}
    assert ("agent", GREETING) in said
    assert ("caller", CALLER_ASKS) in said
    assert ("agent", AGENT_ANSWERS) in said


@pytest.mark.asyncio
async def test_captured_need_is_stored_as_a_lead(profile, settings, store):
    sess, _, _ = await run_call(profile, settings, store)
    detail = store.recent_calls(sess.organization_id, 1)[0]
    assert len(detail["leads"]) == 1
    lead = detail["leads"][0]
    assert lead["intent"] == "book_appointment"
    assert lead["caller_name"] == "Dana"
    assert lead["callback_number"] == "+16175550188"


@pytest.mark.asyncio
async def test_tool_results_are_returned_to_the_model(profile, settings, store):
    _, _, fake = await run_call(profile, settings, store)
    outputs = fake.recorder.tool_outputs
    assert [o["call_id"] for o in outputs] == ["call_0", "call_0"]
    first = json.loads(outputs[0]["output"])
    assert first["status"] == "recorded"
    # After a captured need the model must get the floor back to keep talking.
    assert len(fake.recorder.of_type("response.create")) == 2


@pytest.mark.asyncio
async def test_end_call_hangs_up_and_does_not_ask_for_more_speech(
    profile, settings, store
):
    sess, calls, fake = await run_call(profile, settings, store)
    assert calls.hangups == ["rtc_test_1"]
    # Two response.create total: the greeting and the one after capture. The
    # end_call tool must not trigger a third.
    assert len(fake.recorder.of_type("response.create")) == 2
    assert sess.outcome == "completed"


@pytest.mark.asyncio
async def test_call_record_is_finalised_with_a_cost(profile, settings, store):
    sess, _, _ = await run_call(profile, settings, store)
    record = store.recent_calls(sess.organization_id, 1)[0]
    assert record["ended_at"]
    assert record["outcome"] == "completed"
    assert json.loads(record["summary"])["summary"]
    # Four scripted responses on the mini model: cents, not dollars.
    assert 0 < record["model_cost"] < 0.10
    assert sess.model_cost == pytest.approx(record["model_cost"], abs=1e-6)
    usage = store.query(
        "SELECT event_type, quantity, provider_cost_micros FROM usage_events"
        " WHERE organization_id = ? AND call_id = ? ORDER BY event_type",
        (sess.organization_id, sess.call_id),
    )
    event_types = {event["event_type"] for event in usage}
    assert "twilio.call.duration" in event_types
    assert "openai.realtime.audio_input" in event_types
    assert "openai.realtime.audio_output" in event_types
    assert "openai.transcription.duration" in event_types
    estimated = next(
        event for event in usage if event["event_type"] == "openai.realtime.estimated_cost"
    )
    assert estimated["provider_cost_micros"] > 0


@pytest.mark.asyncio
async def test_transfer_hands_the_caller_to_a_human(profile, settings, store):
    sess, calls, fake = await run_call(profile, settings, store, scenario="transfer")
    assert calls.refers == [("rtc_test_1", f"tel:{profile.transfer_number}")]
    assert sess.outcome == "transferred"
    # The caller is gone, so the agent must not be told to keep talking.
    assert len(fake.recorder.of_type("response.create")) == 1
    assert calls.hangups == []


@pytest.mark.asyncio
async def test_failed_transfer_keeps_the_call_alive(profile, settings, store):
    """If the carrier refuses the transfer, the caller is still on the line."""
    sess, _, fake = await run_call(
        profile, settings, store, scenario="transfer", calls=FailingTransfer()
    )
    assert sess.outcome == "transfer_failed"
    result = json.loads(fake.recorder.tool_outputs[0]["output"])
    assert result["status"] == "failed"
    assert "take a message" in result["instruction"]
    # The agent must get the floor back to apologise, not sit in silence.
    assert len(fake.recorder.of_type("response.create")) == 2


@pytest.fixture
def instant_retry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(session_module, "WS_RETRY_SECONDS", 0.0)


@pytest.mark.asyncio
async def test_call_survives_a_session_that_is_not_attached_yet(
    profile, settings, store, instant_retry
):
    """/accept returns 200 before the session exists, so the first upgrades 404.

    Giving up there is what drops the call while the caller still hears ringing.
    """
    sess, _, fake = await run_call(profile, settings, store, reject_first=3)
    assert fake.rejected == 3
    assert sess.outcome == "completed"
    turns = store.transcript(sess.organization_id, sess.call_id)
    assert ("agent", GREETING) in {(t["role"], t["text"]) for t in turns}


@pytest.mark.asyncio
async def test_giving_up_is_recorded_when_the_session_never_attaches(
    profile, settings, store, instant_retry, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(session_module, "WS_CONNECT_ATTEMPTS", 3)
    sess, _, fake = await run_call(profile, settings, store, reject_first=99)
    assert fake.rejected == 3, "must stop after the configured number of attempts"
    assert sess.outcome == "error"
    assert store.recent_calls(sess.organization_id, 1)[0]["outcome"] == "error"


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_retried(profile, settings, store, instant_retry):
    """Only 404 means 'not ready'. Anything else is a real error, so fail fast."""
    sess, _, fake = await run_call(
        profile, settings, store, reject_first=99, reject_status=401
    )
    assert fake.rejected == 1, "401 must not be retried"
    assert sess.outcome == "error"


@pytest.mark.asyncio
async def test_attach_sends_the_origin_the_api_expects(profile, settings, store):
    """Without this header the sideband endpoint answers 404, which looks
    identical to a call that does not exist and cost hours to track down."""
    _, _, fake = await run_call(profile, settings, store)
    assert fake.handshake_headers.get("Origin") == session_module.WS_ORIGIN


@pytest.mark.asyncio
async def test_transfer_is_unavailable_when_no_number_configured(
    profile, settings, store
):
    profile.raw["contact"]["transfer_to"] = ""
    sess, calls, fake = await run_call(profile, settings, store, scenario="transfer")
    assert calls.refers == []
    result = json.loads(fake.recorder.tool_outputs[0]["output"])
    assert result["status"] == "unavailable"
    assert sess.outcome != "transferred"


@pytest.mark.asyncio
async def test_call_queues_signed_webhook_deliveries(profile, settings, store):
    """A finished call fans out call.completed and lead.created to any endpoint
    the organization has registered."""
    published = BusinessRepository(store).publish(profile)
    store.create_webhook_endpoint(
        published.organization_id,
        "https://hooks.example.test/in",
        "whsec_test_secret",
        None,
        None,
        True,
    )
    sess, _, _ = await run_call(profile, settings, store)

    deliveries = store.query(
        "SELECT e.type AS t, d.status FROM webhook_deliveries d"
        " JOIN webhook_events e ON e.id = d.webhook_event_id"
        " WHERE d.organization_id = ?",
        (sess.organization_id,),
    )
    kinds = {row["t"] for row in deliveries}
    assert kinds == {"call.completed", "lead.created"}
    assert all(row["status"] == "pending" for row in deliveries)


@pytest.mark.asyncio
async def test_call_sends_opted_in_post_call_email(
    profile, settings, store, monkeypatch
):
    from app.domains.calls import notifications

    delivered = []
    monkeypatch.setattr(
        notifications,
        "deliver_call_summary",
        lambda **kwargs: delivered.append(kwargs),
    )
    profile.raw["business"]["notify_email"] = "frontdesk@example.com"

    sess, _, _ = await run_call(profile, settings, store)

    assert len(delivered) == 1
    assert delivered[0]["recipient"] == "frontdesk@example.com"
    assert delivered[0]["call_id"] == sess.call_id
    assert delivered[0]["summary"]["summary"] == ("Caller asked about cleaning prices.")


# -- calendar integration ------------------------------------------------


class FakeCalendar:
    """A CalendarProvider stub the call-flow tests drive directly."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.bookings: list[dict] = []

    def verify(self) -> dict:
        return {"external_account_id": "fake"}

    def available_slots(self, *, start, end, duration_minutes):
        if self.fail:
            raise CalendarProviderError("down", "calendar unreachable", retryable=True)
        first = datetime(2099, 6, 1, 14, 0, tzinfo=UTC)
        return [
            TimeSlot(start=first, end=first + timedelta(minutes=duration_minutes)),
            TimeSlot(
                start=first + timedelta(hours=1),
                end=first + timedelta(hours=1, minutes=duration_minutes),
            ),
        ]

    def create_booking(
        self,
        *,
        start,
        duration_minutes,
        name,
        phone=None,
        email=None,
        notes=None,
        timezone="UTC",
    ):
        if self.fail:
            raise CalendarProviderError("down", "calendar unreachable", retryable=True)
        self.bookings.append(
            {"start": start, "name": name, "phone": phone, "notes": notes}
        )
        return Booking(
            id="bk_fake_1",
            start=start,
            end=start + timedelta(minutes=duration_minutes),
            location="https://meet.example/fake",
        )

    def cancel_booking(self, booking_id, *, reason=None):  # pragma: no cover
        raise NotImplementedError

    def reschedule_booking(self, booking_id, *, start):  # pragma: no cover
        raise NotImplementedError


def test_calendar_tools_are_offered_only_when_connected(profile):
    plain = {t["name"] for t in tool_registry.tool_schemas(profile)}
    assert {"check_availability", "book_appointment"}.isdisjoint(plain)

    with_calendar = {
        t["name"] for t in tool_registry.tool_schemas(profile, calendar=True)
    }
    assert {"check_availability", "book_appointment"} <= with_calendar


@pytest.mark.asyncio
async def test_calendar_booking_happy_path(profile, settings, store):
    published = BusinessRepository(store).publish(profile)
    store.create_webhook_endpoint(
        published.organization_id,
        "https://hooks.example.test/in",
        "whsec_test_secret",
        None,
        None,
        True,
    )
    calendar = FakeCalendar()
    sess, calls, fake = await run_call(
        profile, settings, store, scenario="calendar", calendar=calendar
    )

    assert len(calendar.bookings) == 1
    assert calendar.bookings[0]["name"] == "Dana"

    outputs = [json.loads(o["output"]) for o in fake.recorder.tool_outputs]
    statuses = [o.get("status") for o in outputs]
    assert "ok" in statuses  # check_availability
    assert "booked" in statuses  # book_appointment

    record = store.recent_calls(sess.organization_id, 1)[0]
    assert record["outcome"] == "completed"
    booking_leads = [
        lead for lead in record["leads"] if lead["intent"] == "book_appointment"
    ]
    assert booking_leads and booking_leads[0]["caller_name"] == "Dana"
    assert calls.hangups == ["rtc_test_1"]

    kinds = {
        row["t"]
        for row in store.query(
            "SELECT e.type AS t FROM webhook_deliveries d"
            " JOIN webhook_events e ON e.id = d.webhook_event_id"
            " WHERE d.organization_id = ?",
            (sess.organization_id,),
        )
    }
    assert {"call.completed", "lead.created", "appointment.booked"} <= kinds


@pytest.mark.asyncio
async def test_calendar_failure_degrades_to_a_captured_followup(
    profile, settings, store
):
    calendar = FakeCalendar(fail=True)
    sess, calls, fake = await run_call(
        profile, settings, store, scenario="calendar", calendar=calendar
    )

    outputs = [json.loads(o["output"]) for o in fake.recorder.tool_outputs]
    statuses = [o.get("status") for o in outputs]
    assert "unavailable" in statuses  # check_availability could not reach the provider
    assert "failed" in statuses  # book_appointment fell back

    assert calendar.bookings == []
    record = store.recent_calls(sess.organization_id, 1)[0]
    assert record["outcome"] == "completed"
    followup = [
        lead for lead in record["leads"] if lead["intent"] == "book_appointment"
    ]
    assert followup and "callback" in followup[0]["details"].lower()
    assert calls.hangups == ["rtc_test_1"]


# -- CRM integration ---------------------------------------------------


class FakeCrm:
    def __init__(self, *, match: bool = True) -> None:
        self.match = match
        self.contacts: list = []

    def verify(self) -> dict:
        return {"external_account_id": "portal-1"}

    def find_contact(self, *, phone=None, email=None):
        from app.domains.integrations.base import CrmContact

        return CrmContact(id="hs-1", name="Dana Scully") if self.match else None

    def upsert_contact(self, *, name=None, phone=None, email=None):  # pragma: no cover
        from app.domains.integrations.base import CrmContact

        return CrmContact(id="hs-1", name=name)

    def add_note(self, *, contact_id, body):  # pragma: no cover
        return "note-1"

    def create_task(self, *, contact_id, title, body=None, due_at=None):  # pragma: no cover
        return "task-1"


def _connect_hubspot(store, org_id: str) -> None:
    from datetime import UTC, datetime

    store.upsert_integration_connection(
        org_id,
        "hubspot",
        status="active",
        display_name=None,
        encrypted_credentials="not-read-in-this-path",
        external_account_id="portal-1",
        scopes=None,
        settings="{}",
        last_error=None,
        last_verified_at=datetime.now(UTC).replace(microsecond=0),
    )


def test_crm_tools_are_offered_only_when_connected(profile):
    plain = {t["name"] for t in tool_registry.tool_schemas(profile)}
    assert {"find_customer", "create_follow_up"}.isdisjoint(plain)
    with_crm = {t["name"] for t in tool_registry.tool_schemas(profile, crm=True)}
    assert {"find_customer", "create_follow_up"} <= with_crm


@pytest.mark.asyncio
async def test_crm_recognises_caller_and_queues_a_post_call_sync(
    profile, settings, store
):
    published = BusinessRepository(store).publish(profile)
    _connect_hubspot(store, published.organization_id)

    crm = FakeCrm(match=True)
    sess, calls, fake = await run_call(
        profile, settings, store, scenario="crm", crm=crm
    )

    outputs = [json.loads(o["output"]) for o in fake.recorder.tool_outputs]
    found = next(o for o in outputs if o.get("status") == "found")
    assert found["name"] == "Dana Scully"
    assert any(o.get("status") == "noted" for o in outputs)  # create_follow_up

    record = store.recent_calls(sess.organization_id, 1)[0]
    assert record["outcome"] == "completed"
    assert any("refund" in (lead["details"] or "").lower() for lead in record["leads"])

    jobs = store.query(
        "SELECT provider, status, call_id FROM crm_sync_jobs WHERE organization_id = ?",
        (sess.organization_id,),
    )
    assert jobs == [{"provider": "hubspot", "status": "pending", "call_id": "rtc_test_1"}]
    assert calls.hangups == ["rtc_test_1"]
