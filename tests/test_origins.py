"""Choosing which frontend a user-facing link points back at."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.origins import normalize_origin, origin_from_headers, pick_base_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://app.example.com", "https://app.example.com"),
        ("https://app.example.com/", "https://app.example.com"),
        ("https://App.Example.COM/onboarding", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://localhost:80/x", "http://localhost"),
        ("garbage", "garbage"),
    ],
)
def test_normalize_origin(value: str, expected: str) -> None:
    assert normalize_origin(value) == expected


def test_origin_from_headers_prefers_origin() -> None:
    assert (
        origin_from_headers("https://app.example.com", "https://other.example.com/x")
        == "https://app.example.com"
    )


def test_origin_from_headers_falls_back_to_referer_scheme_host() -> None:
    assert (
        origin_from_headers(None, "https://app.example.com/reset?token=abc")
        == "https://app.example.com"
    )


@pytest.mark.parametrize("origin", [None, "", "null", "NULL"])
def test_origin_from_headers_none_when_unusable(origin: str | None) -> None:
    assert origin_from_headers(origin, None) is None


PRIMARY = "https://voice.example.com"
ALLOWED = (PRIMARY, "https://app.example.com")


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://app.example.com", "https://app.example.com"),
        ("https://app.example.com/", "https://app.example.com"),
        ("https://voice.example.com", PRIMARY),
        ("https://evil.example.com", PRIMARY),  # not allowlisted -> primary
        (None, PRIMARY),
    ],
)
def test_pick_base_url(origin: str | None, expected: str) -> None:
    assert pick_base_url(origin, PRIMARY, ALLOWED) == expected


def test_settings_app_base_urls_dedupes_primary_first() -> None:
    from app.settings import load_settings

    base = replace(
        load_settings(),
        app_base_url="https://voice.example.com",
        extra_base_urls=("https://app.example.com", "https://voice.example.com"),
    )
    assert base.app_base_urls == (
        "https://voice.example.com",
        "https://app.example.com",
    )
    assert base.resolve_base_url("https://app.example.com/x") == "https://app.example.com"
    assert base.resolve_base_url("https://nope.example.com") == "https://voice.example.com"
    assert base.resolve_base_url(None) == "https://voice.example.com"
