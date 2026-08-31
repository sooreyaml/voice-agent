"""Per-call cost estimation.

Rates are US dollars per 1M tokens, taken from the OpenAI pricing page.
They change; check https://developers.openai.com/api/docs/pricing and update.
"""

from __future__ import annotations

from typing import Any

RATES: dict[str, dict[str, float]] = {
    "gpt-realtime-2.1": {
        "audio_in": 32.00,
        "audio_in_cached": 0.40,
        "audio_out": 64.00,
        "text_in": 4.00,
        "text_in_cached": 0.40,
        "text_out": 24.00,
    },
    "gpt-realtime-2.1-mini": {
        "audio_in": 10.00,
        "audio_in_cached": 0.30,
        "audio_out": 20.00,
        "text_in": 0.60,
        "text_in_cached": 0.06,
        "text_out": 2.40,
    },
}


def _rates_for(model: str) -> dict[str, float]:
    if model in RATES:
        return RATES[model]
    # Snapshots look like "gpt-realtime-2.1-mini-2026-01-01". Match the longest
    # name first, otherwise a mini snapshot matches the flagship prefix.
    for known in sorted(RATES, key=len, reverse=True):
        if model.startswith(known):
            return RATES[known]
    return RATES["gpt-realtime-2.1-mini"]


def cost_of_usage(model: str, usage: dict[str, Any]) -> float:
    """Turn one `response.done` usage object into a dollar amount."""
    rates = _rates_for(model)
    inp = usage.get("input_token_details") or {}
    out = usage.get("output_token_details") or {}
    cached = inp.get("cached_tokens_details") or {}

    cached_audio = int(cached.get("audio_tokens") or 0)
    cached_text = int(cached.get("text_tokens") or 0)
    # Cached tokens are also counted in the totals, so subtract them out before
    # billing the rest at the full rate.
    audio_in = max(int(inp.get("audio_tokens") or 0) - cached_audio, 0)
    text_in = max(int(inp.get("text_tokens") or 0) - cached_text, 0)

    total = (
        audio_in * rates["audio_in"]
        + cached_audio * rates["audio_in_cached"]
        + text_in * rates["text_in"]
        + cached_text * rates["text_in_cached"]
        + int(out.get("audio_tokens") or 0) * rates["audio_out"]
        + int(out.get("text_tokens") or 0) * rates["text_out"]
    )
    return total / 1_000_000
