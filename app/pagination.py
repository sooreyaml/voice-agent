"""Opaque cursor helpers shared by the paginated list endpoints.

A cursor is just the last row's sort key(s), base64url-encoded so clients treat
it as a token rather than something to construct.
"""

from __future__ import annotations

import base64
import binascii


class InvalidCursor(ValueError):
    """The client sent a cursor that is not one we issued."""


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("malformed page cursor") from exc
