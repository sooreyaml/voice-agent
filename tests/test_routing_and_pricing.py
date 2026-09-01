"""Number-to-business routing, cost accounting, and webhook hardening."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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


def test_single_published_business_answers_any_number_in_yaml_mode(tmp_path: Path):
    store = Store(tmp_path / "s.sqlite3")
    _publish(store, SECOND_BUSINESS, "second-business")
    # yaml mode passes allow_single_fallback=True so local testing works before
    # a real number is provisioned.
    routed = BusinessRepository(store).find_by_phone_number(
        "+19998887777", allow_single_fallback=True
    )
    assert routed is not None and routed.name == "Second Business"


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
        organization_id = health.json()["businesses"][0]["organization_id"]
        # Call history now lives behind the authenticated management API.
        anon = client.get(f"/api/v1/organizations/{organization_id}/calls")
        assert anon.status_code == 401
        assert anon.json()["error"]["code"] == "not_authenticated"


def test_database_mode_bootstraps_businesses_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import app.main
    from app.settings import settings as real_settings

    database_path = tmp_path / "calls.sqlite3"
    empty_mounted_directory = tmp_path / "mounted-businesses"
    empty_mounted_directory.mkdir()
    monkeypatch.setattr(app.main, "PACKAGED_BUSINESSES_DIR", BUSINESSES)
    monkeypatch.setattr(
        app.main,
        "settings",
        replace(
            real_settings,
            openai_api_key="sk-test",
            openai_webhook_secret="whsec_test",
            database_path=database_path,
            database_url="",
            businesses_dir=empty_mounted_directory,
            business_config_source="database",
        ),
    )

    with TestClient(app.main.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["business_config_source"] == "database"
        assert [business["slug"] for business in health.json()["businesses"]] == [
            "harborview-dental"
        ]

    store = Store(database_path)
    profiles = BusinessRepository(store).list_published()
    assert [profile.slug for profile in profiles] == ["harborview-dental"]
    store.close()
