"""HMAC signing for outbound webhook requests.

Header format mirrors Stripe's so receivers can reuse familiar verification code::

    X-Callagent-Signature: t=1735689600,v1=<hex hmac_sha256(secret, "t.body")>

The receiver recomputes the HMAC over ``"{t}.{raw_request_body}"`` with the
endpoint secret, compares in constant time, and rejects timestamps outside its
tolerance window.
"""

from __future__ import annotations

import hashlib
import hmac

TOLERANCE_SECONDS = 300


def signature_payload(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def compute(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signature_payload(timestamp, body), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(
    secret: str, header: str, body: bytes, *, now: int, tolerance: int = TOLERANCE_SECONDS
) -> bool:
    """Reference verifier — also what the test suite uses."""
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), signature_payload(timestamp, body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))
