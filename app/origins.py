"""Pick which frontend a user-facing link should point at.

One backend serves several frontends (a business app, a consumer app, local
dev, ...). Emails (invite / verify-email / password-reset) and the signup
Stripe redirect must send the user back to the site they came from, so the
request's ``Origin`` (or ``Referer``) is matched against a configured
allowlist. Anything unrecognised falls back to the primary ``APP_BASE_URL`` --
an attacker-supplied ``Origin`` can never redirect a link to their own host.

Pure stdlib on purpose: imported by ``app.settings``.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(value: str) -> str:
    """Reduce a URL to a comparable ``scheme://host[:port]`` key.

    Lower-cases scheme and host, drops the default port, and strips any path,
    query, fragment, or trailing slash. A value without a scheme+host is
    returned trimmed and lower-cased so two malformed entries still compare.
    """

    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.hostname:
        return value.strip().rstrip("/").lower()
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def origin_from_headers(
    origin_header: str | None, referer_header: str | None
) -> str | None:
    """The requesting site, from the ``Origin`` header or, failing that,
    the scheme+host of ``Referer``. ``None`` when neither is usable."""

    if origin_header:
        candidate = origin_header.strip()
        # Browsers send "null" for opaque origins (sandboxed iframes, some
        # privacy modes) -- not a site we can match.
        if candidate and candidate.lower() != "null":
            return candidate
    if referer_header:
        parts = urlsplit(referer_header.strip())
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def pick_base_url(
    origin: str | None, primary: str, allowed: Iterable[str]
) -> str:
    """The configured base URL whose origin matches ``origin``; ``primary``
    when there is no match (missing origin, or one that isn't allowlisted)."""

    if not origin:
        return primary
    target = normalize_origin(origin)
    for candidate in allowed:
        if normalize_origin(candidate) == target:
            return candidate
    return primary
