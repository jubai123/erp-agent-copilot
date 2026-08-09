from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import erp_copilot.domain.entities  # noqa: F401 - register ORM models on Base.metadata
from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Load database URL from application settings (env vars / .env).
_settings = Settings()  # type: ignore[call-arg]
_db_url = _settings.database_url.get_secret_value()
config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url") or "",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
