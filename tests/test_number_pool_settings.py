from __future__ import annotations

from app.settings import load_settings


def test_legacy_pool_target_does_not_enable_automatic_refill(monkeypatch) -> None:
    monkeypatch.delenv("NUMBER_POOL_AUTO_REFILL_ENABLED", raising=False)
    monkeypatch.setenv("NUMBER_POOL_TARGET", "10")

    settings = load_settings()

    assert settings.number_pool_target == 10
    assert settings.number_pool_refill_enabled is False


def test_automatic_refill_requires_switch_and_positive_target(monkeypatch) -> None:
    monkeypatch.setenv("NUMBER_POOL_AUTO_REFILL_ENABLED", "true")
    monkeypatch.setenv("NUMBER_POOL_TARGET", "10")

    assert load_settings().number_pool_refill_enabled is True

    monkeypatch.setenv("NUMBER_POOL_TARGET", "0")

    assert load_settings().number_pool_refill_enabled is False
