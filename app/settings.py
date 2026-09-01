from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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
    twilio_account_sid: str
    twilio_auth_token: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    webhook_timeout_seconds: float
    webhook_max_attempts: int
    api_key_rate_limit_per_minute: int

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

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
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
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
