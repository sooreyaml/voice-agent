from __future__ import annotations


def normalize_e164(value: str) -> str:
    raw = str(value).strip()
    if not raw.startswith("+"):
        raise ValueError("phone numbers must include a leading + and country code")
    digits = "".join(character for character in raw if character.isdigit())
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError("phone numbers must be valid E.164 values")
    return f"+{digits}"
