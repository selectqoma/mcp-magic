import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import settings
from src.db import Base

# Import all models so they're registered
import src.models  # noqa: F401

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline():
    url = settings.database_url
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    connectable = create_async_engine(settings.database_url)
    async with connectable.connect() as connection:
        # Auto-heal: if alembic_version claims migrations ran but tables are gone, reset it.
        # This handles the case where a previous deploy partially failed mid-migration.
        result = await connection.execute(sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'opportunities')"
        ))
        if not result.scalar():
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
            await connection.commit()

        await connection.run_sync(do_run_migrations)
        await connection.commit()  # required for async SQLAlchemy 2.0 — without this DDL is never committed
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
