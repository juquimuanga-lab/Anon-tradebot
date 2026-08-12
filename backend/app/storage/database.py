"""SQLAlchemy async engine/session setup (SQLite for MVP)."""

import os

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect, text

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sqlalchemy.orm import declarative_base

from app.config.settings import settings


Base = declarative_base()


db_path = settings.database_url.split(
    "///"
)[-1]


if db_path and db_path not in (
    ":memory:",
):

    os.makedirs(
        os.path.dirname(db_path)
        or ".",
        exist_ok=True,
    )


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
)


SessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    """
    Initialize database tables and apply lightweight migrations.

    SQLAlchemy create_all() creates missing tables but does not modify
    existing tables. Therefore _migrate_add_missing_columns() handles
    columns introduced after a deployed database already existed.
    """

    from app.storage import models  # noqa: F401

    async with engine.begin() as conn:

        await conn.run_sync(
            Base.metadata.create_all
        )

        await conn.run_sync(
            _migrate_add_missing_columns
        )


def _migrate_add_missing_columns(
    conn,
) -> None:
    """
    Add columns introduced after the database was originally created.

    This function is intentionally idempotent and safe to run on every
    application startup.

    Current migrations:

        orders.rule_id
        orders.owner_user_id
        positions.source
    """

    inspector = inspect(
        conn
    )

    tables = set(
        inspector.get_table_names()
    )

    # ---------------------------------------------------------------
    # Orders
    # ---------------------------------------------------------------

    if "orders" in tables:

        existing_order_columns = {
            column["name"]
            for column in inspector.get_columns(
                "orders"
            )
        }

        if "rule_id" not in existing_order_columns:

            conn.execute(
                text(
                    """
                    ALTER TABLE orders
                    ADD COLUMN rule_id INTEGER
                    """
                )
            )

        if (
            "owner_user_id"
            not in existing_order_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE orders
                    ADD COLUMN owner_user_id INTEGER
                    """
                )
            )

    # ---------------------------------------------------------------
    # Positions
    # ---------------------------------------------------------------
    #
    # New Pump.fun source-aware execution requires every position to
    # remember where it originated.
    #
    # Existing positions are assumed to be Anoncoin/Meteora positions,
    # so they receive the safe legacy default.
    # ---------------------------------------------------------------

    if "positions" in tables:

        existing_position_columns = {
            column["name"]
            for column in inspector.get_columns(
                "positions"
            )
        }

        if (
            "source"
            not in existing_position_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE positions
                    ADD COLUMN source VARCHAR
                    DEFAULT 'anoncoin_onchain'
                    """
                )
            )


@asynccontextmanager
async def async_session_scope(
) -> AsyncIterator[AsyncSession]:

    async with SessionLocal() as session:

        yield session
