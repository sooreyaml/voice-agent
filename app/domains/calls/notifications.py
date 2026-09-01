from __future__ import annotations

import hashlib
import html
import logging
from typing import Any

from app.domains.email.service import send_email

logger = logging.getLogger(__name__)

_SUMMARY_FIELDS = (
    ("summary", "Summary"),
    ("caller_wants", "Caller wants"),
    ("action_required", "Action required"),
    ("sentiment", "Sentiment"),
    ("unanswered", "Unanswered"),
)


def _clean(value: Any, default: str = "Not provided") -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or default


def deliver_call_summary(
    *,
    recipient: str,
    business_name: str,
    call_id: str,
    from_number: str,
    outcome: str,
    summary: Any,
    resend_api_key: str,
    resend_from_email: str,
) -> str | None:
    """Email a concise post-call handover to a configured business address."""
    if not resend_api_key or not resend_from_email:
        logger.info(
            "post-call email for %s not sent; Resend is not configured", call_id
        )
        return None

    business = _clean(business_name, "your business")
    caller = _clean(from_number, "Unknown caller")
    call_outcome = _clean(outcome, "unknown")
    summary_values = summary if isinstance(summary, dict) else {"summary": summary}
    values = [
        (label, _clean(summary_values.get(key))) for key, label in _SUMMARY_FIELDS
    ]

    detail_html = "".join(
        f"<dt><strong>{html.escape(label)}</strong></dt><dd>{html.escape(value)}</dd>"
        for label, value in values
    )
    message_html = (
        f"<h1>New call for {html.escape(business)}</h1>"
        f"<p><strong>Caller:</strong> {html.escape(caller)}<br>"
        f"<strong>Outcome:</strong> {html.escape(call_outcome)}</p>"
        f"<dl>{detail_html}</dl>"
    )
    details_text = "\n".join(f"{label}: {value}" for label, value in values)
    message_text = (
        f"New call for {business}\n\n"
        f"Caller: {caller}\nOutcome: {call_outcome}\n\n{details_text}"
    )
    call_digest = hashlib.sha256(call_id.encode()).hexdigest()
    return send_email(
        api_key=resend_api_key,
        sender=resend_from_email,
        recipient=recipient,
        subject=f"New call for {business}: {caller}",
        html=message_html,
        text=message_text,
        idempotency_key=f"call-summary-{call_digest}",
    )
