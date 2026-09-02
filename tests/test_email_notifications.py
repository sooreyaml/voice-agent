from __future__ import annotations

from app.domains.auth import notifications as auth_notifications
from app.domains.auth.models import EmailTokenPurpose
from app.domains.calls import notifications as call_notifications
from app.domains.email import service as email_service
from app.domains.organizations import notifications as organization_notifications


def test_email_service_sends_through_resend(monkeypatch):
    captured = {}

    def fake_send(params, options):
        captured.update(params=params, options=options)
        return {"id": "email_123"}

    monkeypatch.setattr(email_service.resend.Emails, "send", fake_send)

    message_id = email_service.send_email(
        api_key="re_test",
        sender="Call Agent <mail@example.com>",
        recipient="owner@example.com",
        subject="Test",
        html="<p>Hello</p>",
        text="Hello",
        idempotency_key="test-message",
    )

    assert message_id == "email_123"
    assert email_service.resend.api_key == "re_test"
    assert captured == {
        "params": {
            "from": "Call Agent <mail@example.com>",
            "to": ["owner@example.com"],
            "subject": "Test",
            "html": "<p>Hello</p>",
            "text": "Hello",
        },
        "options": {"idempotency_key": "test-message"},
    }


def test_auth_notification_builds_verification_code_email(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        auth_notifications,
        "send_email",
        lambda **kwargs: captured.update(kwargs) or "email_auth",
    )

    result = auth_notifications.deliver_email_verification_code(
        email="owner@example.com",
        code="048213",
        resend_api_key="re_test",
        resend_from_email="Call Agent <mail@example.com>",
    )

    assert result == "email_auth"
    assert captured["recipient"] == "owner@example.com"
    assert captured["subject"] == "Your Call Agent verification code"
    assert "048213" in captured["html"]
    assert "048213" in captured["text"]
    # The raw code never lands verbatim in the idempotency key.
    assert "048213" not in captured["idempotency_key"]
    # No verification link anymore.
    assert "http" not in captured["text"]


def test_password_reset_notification_builds_link_email(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        auth_notifications,
        "send_email",
        lambda **kwargs: captured.update(kwargs) or "email_reset",
    )

    result = auth_notifications.deliver_email_token(
        email="owner@example.com",
        purpose=EmailTokenPurpose.RESET_PASSWORD,
        raw_token="token+/value",
        base_url="https://app.example.com",
        resend_api_key="re_test",
        resend_from_email="Call Agent <mail@example.com>",
    )

    assert result == "email_reset"
    assert captured["subject"] == "Reset your Call Agent password"
    assert "https://app.example.com/reset-password?token=token%2B%2Fvalue" in (
        captured["html"]
    )


def test_invitation_email_escapes_organization_name(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        organization_notifications,
        "send_email",
        lambda **kwargs: captured.update(kwargs) or "email_invite",
    )

    organization_notifications.deliver_invitation(
        email="member@example.com",
        organization_name="Example <script>alert(1)</script>\nClinic",
        role="member",
        raw_token="invite-token",
        base_url="https://app.example.com",
        resend_api_key="re_test",
        resend_from_email="Call Agent <mail@example.com>",
    )

    assert "<script>" not in captured["html"]
    assert "&lt;script&gt;" in captured["html"]
    assert "\n" not in captured["subject"]
    assert captured["recipient"] == "member@example.com"


def test_call_summary_email_contains_handover_not_transcript(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        call_notifications,
        "send_email",
        lambda **kwargs: captured.update(kwargs) or "email_call",
    )

    result = call_notifications.deliver_call_summary(
        recipient="frontdesk@example.com",
        business_name="Example Dental",
        call_id="rtc_123",
        from_number="+441234567890",
        outcome="completed",
        summary={
            "summary": "Asked about prices.",
            "caller_wants": "A check-up",
            "action_required": "Call back",
            "sentiment": "positive",
            "unanswered": "none",
        },
        resend_api_key="re_test",
        resend_from_email="Call Agent <mail@example.com>",
    )

    assert result == "email_call"
    assert captured["recipient"] == "frontdesk@example.com"
    assert "Asked about prices." in captured["text"]
    assert "Call back" in captured["text"]
    assert captured["idempotency_key"].startswith("call-summary-")


def test_auth_notification_logs_link_when_resend_is_not_configured(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(
        auth_notifications,
        "send_email",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    result = auth_notifications.deliver_email_token(
        email="owner@example.com",
        purpose=EmailTokenPurpose.RESET_PASSWORD,
        raw_token="local-token",
        base_url="http://localhost:8000",
        resend_api_key="",
        resend_from_email="",
    )

    assert result is None
    assert "local-token" in caplog.text
