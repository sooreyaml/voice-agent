from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.billing.services import spend
from app.settings import load_settings

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"


def test_billing_is_off_by_default() -> None:
    assert load_settings().billing_enabled is False


def test_billing_can_be_enabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "true")

    assert load_settings().billing_enabled is True


def test_disabled_billing_bypasses_spend_checks(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("spend status must not be queried")

    monkeypatch.setattr(spend, "spend_limit_status", fail_if_called)

    assert spend.call_is_allowed(None, "org_123", billing_enabled=False) is True


def test_billing_routes_are_registered_but_404_while_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main

    settings = replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        resend_api_key="",
        resend_from_email="",
        billing_enabled=False,
    )
    monkeypatch.setattr(app.main, "settings", settings)
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        # Present in the schema, but every billing route is a 404.
        assert client.get("/api/v1/billing/plans").status_code == 404
        assert (
            client.post(
                "/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"}
            ).status_code
            == 404
        )
