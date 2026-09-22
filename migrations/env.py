"""Alembic environment configuration for the Yauwa ledger schema.

This file tells Alembic:
  - where the SQLModel metadata lives (so autogenerate can diff the models)
  - how to reach the database (reusing the app's engine, so DATABASE_URL
    and connection pool settings stay in one place)
  - to import every model module, so all tables register into
    SQLModel.metadata before Alembic inspects it
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

# Importing every model module is required. Without these imports, the
# model classes never register into SQLModel.metadata, and Alembic would
# autogenerate an empty migration.
from app.models import item, sale, trader  # noqa: F401
from app.db.session import get_engine
from app.config import get_database_settings

# Alembic Config object; provides access to values in alembic.ini.
config = context.config

# Set up Python logging from the ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata Alembic compares against the live database.
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection).

    Emits the SQL to stdout instead of executing it. Useful for
    generating SQL scripts to hand to a DBA.
    """
    url = str(get_database_settings().database_url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    Reuses the app's existing SQLModel engine, so DATABASE_URL, driver,
    and pool settings stay consistent between the app and migrations.
    """
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()