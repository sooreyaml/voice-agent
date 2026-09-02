from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.origins import is_loopback_origin, pick_base_url

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: str) -> Path:
    raw = os.environ.get(name) or default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = (os.environ.get(name) or default).strip().lower()
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise RuntimeError(f"{name} must be one of: {choices}")
    return value


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_webhook_secret: str
    openai_project_id: str
    realtime_model: str
    realtime_voice: str
    transcribe_caller: bool
    transcribe_model: str
    summary_model: str
    businesses_dir: Path
    database_path: Path
    database_url: str
    redis_url: str
    notify_webhook_url: str
    environment: str
    auth_session_secret: str
    seed_api_token: str
    integration_encryption_key: str
    require_email_verification: bool
    app_base_url: str
    resend_api_key: str
    resend_from_email: str
    twilio_account_sid: str
    twilio_auth_token: str
    billing_enabled: bool
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_id: str
    stripe_meter_event_name: str
    default_billing_plan_code: str
    number_pool_target: int
    number_pool_country: str
    number_pool_number_type: str
    number_pool_sms_enabled: bool
    number_pool_bundle_sid: str
    number_pool_address_sid: str
    default_timezone: str
    signup_checkout_grace_hours: int
    dunning_grace_days: int
    number_quarantine_days: int
    webhook_timeout_seconds: float
    webhook_max_attempts: int
    api_key_rate_limit_per_minute: int
    # Frontend origins allowed in user-facing links (emails, Stripe redirects)
    # in addition to app_base_url. Read app_base_urls / resolve_base_url.
    extra_base_urls: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def app_base_urls(self) -> tuple[str, ...]:
        """Every allowed frontend base URL, primary first, de-duplicated."""
        return tuple(dict.fromkeys((self.app_base_url, *self.extra_base_urls)))

    def resolve_base_url(self, origin: str | None) -> str:
        """The configured base URL matching a request's origin, else the
        primary ``app_base_url``. ``origin`` comes from
        ``app.origins.origin_from_headers``.

        Outside production, a loopback origin (a frontend dev running against a
        shared staging backend) is honoured even though it is not in the
        allowlist -- such a link only resolves on that developer's machine.
        """
        if origin and not self.is_production and is_loopback_origin(origin):
            return origin.rstrip("/")
        return pick_base_url(origin, self.app_base_url, self.app_base_urls)

    @property
    def cookie_secure(self) -> bool:
        """Send auth cookies only over HTTPS outside local development."""
        return self.environment != "development"

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"

    @property
    def sip_uri(self) -> str:
        """The address a SIP trunk should send inbound calls to."""
        return f"sip:{self.openai_project_id}@sip.api.openai.com;transport=tls"

    @property
    def database_target(self) -> str | Path:
        """Postgres when a URL is configured, otherwise a local SQLite file.

        Hosts with an ephemeral filesystem set DATABASE_URL, so preferring it
        keeps call history from vanishing on the next deploy.
        """
        return self.database_url or self.database_path

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(n.upper() for n in missing)
                + ". Copy .env.example to .env and fill it in."
            )


def load_settings() -> Settings:
    return Settings(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_webhook_secret=os.environ.get("OPENAI_WEBHOOK_SECRET", ""),
        openai_project_id=os.environ.get("OPENAI_PROJECT_ID", ""),
        realtime_model=os.environ.get("REALTIME_MODEL", "gpt-realtime-2.1-mini"),
        realtime_voice=os.environ.get("REALTIME_VOICE", "marin"),
        transcribe_caller=_flag("TRANSCRIBE_CALLER", True),
        transcribe_model=os.environ.get("TRANSCRIBE_MODEL", "whisper-1"),
        summary_model=os.environ.get("SUMMARY_MODEL", "gpt-5.6-luna"),
        businesses_dir=_path("BUSINESSES_DIR", "businesses"),
        database_path=_path("DATABASE_PATH", "data/calls.sqlite3"),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        redis_url=os.environ.get("REDIS_URL", "").strip(),
        notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL", ""),
        environment=_choice(
            "ENVIRONMENT", "development", {"development", "staging", "production"}
        ),
        auth_session_secret=os.environ.get("AUTH_SESSION_SECRET", "").strip(),
        seed_api_token=os.environ.get("SEED_API_TOKEN", "").strip(),
        integration_encryption_key=os.environ.get(
            "INTEGRATION_ENCRYPTION_KEY", ""
        ).strip(),
        require_email_verification=_flag("REQUIRE_EMAIL_VERIFICATION", False),
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip(
            "/"
        ),
        extra_base_urls=tuple(
            u.rstrip("/")
            for u in re.split(r"[\s,]+", os.environ.get("APP_BASE_URLS", ""))
            if u.strip()
        ),
        resend_api_key=os.environ.get("RESEND_API_KEY", "").strip(),
        resend_from_email=os.environ.get("RESEND_FROM_EMAIL", "").strip(),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        billing_enabled=_flag("BILLING_ENABLED", False),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
        stripe_price_id=os.environ.get("STRIPE_PRICE_ID", "").strip(),
        stripe_meter_event_name=os.environ.get(
            "STRIPE_METER_EVENT_NAME", "call_seconds"
        ).strip(),
        default_billing_plan_code=os.environ.get(
            "DEFAULT_BILLING_PLAN_CODE", "starter"
        ).strip()
        or "starter",
        number_pool_target=_int(
            "NUMBER_POOL_TARGET", 10, minimum=0, maximum=10_000
        ),
        number_pool_country=(
            os.environ.get("NUMBER_POOL_COUNTRY", "GB").strip().upper() or "GB"
        ),
        number_pool_number_type=(
            os.environ.get("NUMBER_POOL_NUMBER_TYPE", "mobile").strip() or "mobile"
        ),
        number_pool_sms_enabled=_flag("NUMBER_POOL_SMS_ENABLED", True),
        number_pool_bundle_sid=os.environ.get("NUMBER_POOL_BUNDLE_SID", "").strip(),
        number_pool_address_sid=os.environ.get("NUMBER_POOL_ADDRESS_SID", "").strip(),
        default_timezone=(
            os.environ.get("DEFAULT_TIMEZONE", "Europe/London").strip()
            or "Europe/London"
        ),
        signup_checkout_grace_hours=_int(
            "SIGNUP_CHECKOUT_GRACE_HOURS", 24, minimum=1, maximum=720
        ),
        dunning_grace_days=_int(
            "DUNNING_GRACE_DAYS", 7, minimum=0, maximum=90
        ),
        number_quarantine_days=_int(
            "NUMBER_QUARANTINE_DAYS", 30, minimum=0, maximum=365
        ),
        webhook_timeout_seconds=float(
            _int("WEBHOOK_TIMEOUT_SECONDS", 10, minimum=1, maximum=60)
        ),
        webhook_max_attempts=_int(
            "WEBHOOK_MAX_ATTEMPTS", 6, minimum=1, maximum=20
        ),
        api_key_rate_limit_per_minute=_int(
            "API_KEY_RATE_LIMIT_PER_MINUTE", 120, minimum=0, maximum=100_000
        ),
    )


settings = load_settings()
