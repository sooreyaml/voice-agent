from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.database import Base
from app.domains.api_keys import models as api_key_models  # noqa: F401
from app.domains.audit import models as audit_models  # noqa: F401
from app.domains.auth import models as auth_models  # noqa: F401
from app.domains.billing import models as billing_models  # noqa: F401
from app.domains.businesses import models as business_models  # noqa: F401
from app.domains.calls import models as call_models  # noqa: F401
from app.domains.integrations import models as integration_models  # noqa: F401
from app.domains.organizations import models as organization_models  # noqa: F401
from app.domains.privacy import models as privacy_models  # noqa: F401
from app.domains.telephony import models as telephony_models  # noqa: F401
from app.domains.tenancy import models as tenancy_models  # noqa: F401
from app.domains.webhooks import models as webhook_models  # noqa: F401

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
