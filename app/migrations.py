from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def _is_postgres_url(target: str | Path) -> bool:
    return isinstance(target, str) and target.startswith(POSTGRES_PREFIXES)


def sqlalchemy_url(target: str | Path) -> str:
    if _is_postgres_url(target):
        url = str(target)
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url.removeprefix("postgres://")
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return f"sqlite:///{Path(target).resolve()}"


def upgrade_database(target: str | Path) -> None:
    if not _is_postgres_url(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url(target))
    command.upgrade(config, "head")
