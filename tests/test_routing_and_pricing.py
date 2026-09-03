"""Number-to-business routing, cost accounting, and webhook hardening."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app.business import BusinessProfile, load_profiles, render_instructions
from app.domains.businesses.repository import BusinessRepository
from app.pricing import cost_of_usage
from app.store import Store

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"

USAGE = {
    "input_token_details": {
        "audio_tokens": 600,
        "text_tokens": 1200,
        "cached_tokens_details": {"audio_tokens": 0, "text_tokens": 1000},
    },
    "output_token_details": {"audio_tokens": 1200, "text_tokens": 20},
}

SECOND_BUSINESS = """
business:
  name: Second Business
  timezone: Europe/London
  phone_numbers: ["+442071234567"]
agent:
  name: Ava
  greeting: "Hello from the second business."
"""


def _publish(store: Store, yaml_text: str, slug: str) -> BusinessProfile:
    profile = BusinessProfile(raw=yaml.safe_load(yaml_text), slug=slug)
    return BusinessRepository(store).publish(profile)


def test_single_published_business_does_not_answer_an_unassigned_number(
    tmp_path: Path,
):
    store = Store(tmp_path / "s.sqlite3")
    _publish(store, SECOND_BUSINESS, "second-business")
    routed = BusinessRepository(store).find_by_phone_number("+19998887777")
    assert routed is None


def test_routing_ignores_spacing_in_the_dialled_number(tmp_path: Path):
    store = Store(tmp_path / "s.sqlite3")
    _publish(store, SECOND_BUSINESS, "second-business")
    routed = BusinessRepository(store).find_by_phone_number("+44 207 123 4567")
    assert routed is not None and routed.name == "Second Business"


def test_unknown_number_is_refused_when_several_businesses_exist(tmp_path: Path):
    store = Store(tmp_path / "s.sqlite3")
    _publish(store, SECOND_BUSINESS, "second-business")
    _publish(
        store,
        SECOND_BUSINESS.replace("Second Business", "Third").replace(
            "+442071234567", "+442079999999"
        ),
        "third",
    )
    repo = BusinessRepository(store)
    assert repo.find_by_phone_number("+15550000000") is None
    matched = repo.find_by_phone_number("+442079999999")
    assert matched is not None and matched.name == "Third"


def test_unreadable_config_is_skipped_not_fatal(tmp_path: Path):
    (tmp_path / "good.yaml").write_text(SECOND_BUSINESS)
    (tmp_path / "broken.yaml").write_text("business: {name: [unclosed\n")
    profiles = load_profiles(tmp_path)
    assert [p.name for p in profiles] == ["Second Business"]


def test_missing_businesses_dir_fails_loudly(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_profiles(tmp_path / "nope")


def test_prompt_refuses_to_invent_and_knows_the_time():
    profile = load_profiles(BUSINESSES)[0]
    prompt = render_instructions(profile)
    assert "Never guess" in prompt
    assert "It is currently" in prompt
    # Prices must reach the prompt verbatim so the agent can quote them.
    assert "$120" in prompt
    # And the free-prose block must survive untouched.
    assert "Dr. Elena Ruiz" in prompt


@pytest.mark.parametrize(
    "model,expected_cents",
    [("gpt-realtime-2.1", 9.7), ("gpt-realtime-2.1-mini", 3.0)],
)
def test_cost_matches_published_rates(model: str, expected_cents: float):
    assert cost_of_usage(model, USAGE) * 100 == pytest.approx(expected_cents, abs=0.1)


def test_dated_mini_snapshot_is_not_billed_as_flagship():
    """Regression: 'mini-2026-03-01' also starts with the flagship model name."""
    mini = cost_of_usage("gpt-realtime-2.1-mini", USAGE)
    assert cost_of_usage("gpt-realtime-2.1-mini-2026-03-01", USAGE) == mini


def test_cached_audio_is_not_billed_twice():
    """Cached tokens appear in the totals, so they must be subtracted out."""
    uncached = {
        "input_token_details": {
            "audio_tokens": 1000,
            "text_tokens": 0,
            "cached_tokens_details": {"audio_tokens": 0, "text_tokens": 0},
        },
        "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
    }
    cached = {
        "input_token_details": {
            "audio_tokens": 1000,
            "text_tokens": 0,
            "cached_tokens_details": {"audio_tokens": 1000, "text_tokens": 0},
        },
        "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
    }
    assert cost_of_usage("gpt-realtime-2.1-mini", cached) < cost_of_usage(
        "gpt-realtime-2.1-mini", uncached
    )


def test_store_survives_a_call_with_no_conversation(tmp_path: Path):
    store = Store(tmp_path / "s.sqlite3")
    organization_id = store.ensure_organization("biz", "Biz")
    store.start_call(organization_id, "c1", "Biz", "+1", "+2")
    store.finish_call(organization_id, "c1", "no_conversation", "", 0.0)
    record = store.recent_calls(organization_id, 1)[0]
    assert record["outcome"] == "no_conversation"
    assert record["leads"] == []


def test_unsigned_webhook_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import app.main
    from app.settings import settings as real_settings

    # Settings are read from the environment once at import time, so patching
    # the object is what actually reaches the app; setting env vars here would
    # depend on whether another test imported app.settings first.
    monkeypatch.setattr(
        app.main,
        "settings",
        replace(
            real_settings,
            openai_api_key="sk-test",
            openai_webhook_secret="whsec_test",
            database_path=tmp_path / "calls.sqlite3",
            database_url="",
        ),
    )
    with TestClient(app.main.app) as client:
        assert client.post("/openai/webhook", content=b"{}").status_code == 400
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["businesses"] == []
        # Call history now lives behind the authenticated management API.
        anon = client.get("/api/v1/organizations/not-seeded/calls")
        assert anon.status_code == 401
        assert anon.json()["error"]["code"] == "not_authenticated"


def test_empty_database_starts_without_importing_business_templates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import app.main
    from app.settings import settings as real_settings

    database_path = tmp_path / "calls.sqlite3"
    monkeypatch.setattr(
        app.main,
        "settings",
        replace(
            real_settings,
            openai_api_key="sk-test",
            openai_webhook_secret="whsec_test",
            database_path=database_path,
            database_url="",
            businesses_dir=BUSINESSES,
        ),
    )

    with TestClient(app.main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["businesses"] == []

    store = Store(database_path)
    profiles = BusinessRepository(store).list_published()
    assert profiles == []
    store.close()


# -- dialled-number extraction from SIP headers -----------------------
#
# Twilio's SIP trunk points at sip:<project_id>@sip.api.openai.com, so on real
# calls the webhook's "To" header is the OpenAI project id and the number the
# caller actually dialled has to be recovered from another header.


def _headers(**pairs: str) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in pairs.items()]


def test_project_id_in_the_to_header_is_not_mistaken_for_a_number():
    from app.main import _dialed_number

    headers = _headers(
        From="sip:+447300048109@sip.twilio.com",
        To="sip:proj_nqEBsZOBduNEP8ex5E9fLuo6@sip.api.openai.com",
    )
    assert _dialed_number(headers) == ""


def test_dialled_number_is_read_from_the_to_header_when_present():
    from app.main import _dialed_number

    headers = _headers(To="sip:+18005551212@sip.example.com")
    assert _dialed_number(headers) == "+18005551212"


@pytest.mark.parametrize(
    "name,value",
    [
        ("Diversion", "<sip:+441616960976@sip.twilio.com>;reason=unconditional"),
        ("X-Original-To", "sip:+441616960976@sip.twilio.com"),
        ("P-Called-Party-ID", '"Front Desk" <sip:+441616960976@example.com>'),
        ("To", "+441616960976"),
    ],
)
def test_dialled_number_is_recovered_from_fallback_headers(name: str, value: str):
    from app.main import _dialed_number

    headers = _headers(
        From="sip:+447300048109@sip.twilio.com",
        To="sip:proj_abc123@sip.api.openai.com",
    )
    headers.append({"name": name, "value": value})
    assert _dialed_number(headers) == "+441616960976"


def test_the_to_header_wins_over_a_later_fallback_header():
    from app.main import _dialed_number

    headers = _headers(To="sip:+18005551212@sip.example.com")
    headers.append({"name": "Diversion", "value": "<sip:+441616960976@x>"})
    assert _dialed_number(headers) == "+18005551212"


def test_dialled_number_is_empty_when_no_header_carries_one():
    from app.main import _dialed_number

    assert _dialed_number([]) == ""
    assert _dialed_number(_headers(From="sip:+447300048109@sip.twilio.com")) == ""


def test_sole_published_business_only_answers_when_routing_is_unambiguous(
    tmp_path: Path,
):
    from app.main import _sole_published_business

    store = Store(tmp_path / "s.sqlite3")
    repo = BusinessRepository(store)
    assert _sole_published_business(repo) is None

    _publish(store, SECOND_BUSINESS, "second-business")
    only = _sole_published_business(repo)
    assert only is not None and only.name == "Second Business"

    _publish(
        store,
        SECOND_BUSINESS.replace("Second Business", "Third").replace(
            "+442071234567", "+442079999999"
        ),
        "third",
    )
    assert _sole_published_business(repo) is None


# -- the inbound call webhook actually routes the call ----------------


class _StubCalls:
    """Records the SIP-leg decisions the webhook makes."""

    def __init__(self) -> None:
        self.accepted: list[str] = []
        self.rejected: list[tuple[str, int]] = []

    async def accept(self, call_id: str, session_config: dict) -> None:
        self.accepted.append(call_id)

    async def reject(self, call_id: str, status_code: int = 603) -> None:
        self.rejected.append((call_id, status_code))

    async def close(self) -> None:  # called on lifespan shutdown
        pass


class _NoVerifyOpenAI:
    """Skips webhook signature verification; parses the posted body as-is."""

    class _Webhooks:
        def unwrap(self, body: bytes, headers: dict) -> SimpleNamespace:
            payload = json.loads(body)
            data = payload.get("data", {})
            return SimpleNamespace(
                type=payload.get("type"),
                data=SimpleNamespace(
                    call_id=data.get("call_id"),
                    sip_headers=data.get("sip_headers", []),
                ),
            )

    def __init__(self) -> None:
        self.webhooks = self._Webhooks()


@pytest.fixture
def webhook_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main
    from app.settings import settings as real_settings

    monkeypatch.setattr(
        app.main,
        "settings",
        replace(
            real_settings,
            openai_api_key="sk-test",
            openai_webhook_secret="whsec_test",
            database_path=tmp_path / "calls.sqlite3",
            database_url="",
            businesses_dir=BUSINESSES,
            environment="development",
        ),
    )

    async def _noop_run(self) -> None:  # the session's socket work is out of scope
        return None

    monkeypatch.setattr(app.main.CallSession, "run", _noop_run)

    with TestClient(app.main.app) as client:
        client.app.state.calls = _StubCalls()
        client.app.state.openai = _NoVerifyOpenAI()
        yield client


def _incoming_call(call_id: str, headers: list[dict[str, str]]) -> bytes:
    return json.dumps(
        {
            "type": "realtime.call.incoming",
            "data": {"call_id": call_id, "sip_headers": headers},
        }
    ).encode()


def test_webhook_routes_on_the_number_in_a_fallback_header(webhook_client: TestClient):
    client = webhook_client
    _publish(client.app.state.store, SECOND_BUSINESS, "second-business")
    _publish(
        client.app.state.store,
        SECOND_BUSINESS.replace("Second Business", "Third").replace(
            "+442071234567", "+442079999999"
        ),
        "third",
    )

    body = _incoming_call(
        "rtc_1",
        _headers(
            From="sip:+447300048109@sip.twilio.com",
            To="sip:proj_abc123@sip.api.openai.com",
        )
        + [{"name": "Diversion", "value": "<sip:+442071234567@sip.twilio.com>"}],
    )
    resp = client.post("/openai/webhook", content=body)

    assert resp.status_code == 200
    assert client.app.state.calls.accepted == ["rtc_1"]
    assert client.app.state.calls.rejected == []


def test_webhook_falls_back_to_the_only_business_when_no_number_arrives(
    webhook_client: TestClient,
):
    client = webhook_client
    _publish(client.app.state.store, SECOND_BUSINESS, "second-business")

    body = _incoming_call(
        "rtc_2",
        _headers(
            From="sip:+447300048109@sip.twilio.com",
            To="sip:proj_abc123@sip.api.openai.com",
        ),
    )
    resp = client.post("/openai/webhook", content=body)

    assert resp.status_code == 200
    assert client.app.state.calls.accepted == ["rtc_2"]


def test_webhook_declines_when_the_number_is_missing_and_routing_is_ambiguous(
    webhook_client: TestClient,
):
    client = webhook_client
    _publish(client.app.state.store, SECOND_BUSINESS, "second-business")
    _publish(
        client.app.state.store,
        SECOND_BUSINESS.replace("Second Business", "Third").replace(
            "+442071234567", "+442079999999"
        ),
        "third",
    )

    body = _incoming_call(
        "rtc_3",
        _headers(To="sip:proj_abc123@sip.api.openai.com"),
    )
    resp = client.post("/openai/webhook", content=body)

    assert resp.status_code == 200
    assert client.app.state.calls.accepted == []
    assert client.app.state.calls.rejected == [("rtc_3", 404)]
