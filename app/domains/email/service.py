from __future__ import annotations

import logging

import resend

logger = logging.getLogger(__name__)


def send_email(
    *,
    api_key: str,
    sender: str,
    recipient: str,
    subject: str,
    html: str,
    text: str,
    idempotency_key: str,
) -> str:
    """Send one transactional email and return the Resend message id."""
    if not api_key or not sender:
        raise RuntimeError("Resend email delivery is not configured")

    resend.api_key = api_key
    response = resend.Emails.send(
        {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": text,
        },
        {"idempotency_key": idempotency_key},
    )
    message_id = str(response["id"])
    logger.info("sent Resend email %s to %s", message_id, recipient)
    return message_id
