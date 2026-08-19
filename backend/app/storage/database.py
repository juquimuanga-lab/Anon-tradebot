"""SQLAlchemy async engine/session setup (SQLite for MVP)."""

import os

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, inspect, text

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
    connect_args={
        "timeout": 30,
    },
)


# SQLite is used for the MVP. WAL mode allows the scanner/position monitor
# to read while another short transaction is committing, and busy_timeout
# prevents transient lock contention from immediately surfacing as
# "database is locked" during fast launch bursts.
@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    if db_path and db_path != ":memory:":
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


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
        bot_state.anoncoin_trading_enabled
        bot_state.pumpfun_trading_enabled
        bot_state.fourmeme_trading_enabled
        rules.platform
        rules.max_buy_size_bnb
        rules.qualify_score_threshold
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

        if (
            "defensive_exit_done"
            not in existing_position_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE positions
                    ADD COLUMN defensive_exit_done BOOLEAN
                    DEFAULT 0
                    """
                )
            )

    # ---------------------------------------------------------------
    # Rules
    # ---------------------------------------------------------------

    if "rules" in tables:
        existing_rule_columns = {
            column["name"]
            for column in inspector.get_columns("rules")
        }

        if "platform" not in existing_rule_columns:
            conn.execute(text(
                """ALTER TABLE rules ADD COLUMN platform VARCHAR DEFAULT 'solana'"""
            ))

        if "max_buy_size_bnb" not in existing_rule_columns:
            conn.execute(text(
                """ALTER TABLE rules ADD COLUMN max_buy_size_bnb FLOAT DEFAULT 0.01"""
            ))

        if "qualify_score_threshold" not in existing_rule_columns:
            conn.execute(text(
                """ALTER TABLE rules ADD COLUMN qualify_score_threshold FLOAT DEFAULT 52.0"""
            ))

    # ---------------------------------------------------------------
    # Bot state
    # ---------------------------------------------------------------
    #
    # Per-source trading toggles, so Anoncoin and Pump.fun can each be
    # switched on/off independently instead of only the all-or-nothing
    # trading_enabled kill switch.
    #
    # Existing deployments default both to enabled, preserving current
    # behaviour (both sources trade) until an admin explicitly narrows
    # it with /disableanoncoin or /disablepumpfun.
    # ---------------------------------------------------------------

    if "bot_state" in tables:

        existing_bot_state_columns = {
            column["name"]
            for column in inspector.get_columns(
                "bot_state"
            )
        }

        if (
            "anoncoin_trading_enabled"
            not in existing_bot_state_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE bot_state
                    ADD COLUMN anoncoin_trading_enabled BOOLEAN
                    DEFAULT 1
                    """
                )
            )

        if (
            "pumpfun_trading_enabled"
            not in existing_bot_state_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE bot_state
                    ADD COLUMN pumpfun_trading_enabled BOOLEAN
                    DEFAULT 1
                    """
                )
            )

        if (
            "fourmeme_trading_enabled"
            not in existing_bot_state_columns
        ):

            conn.execute(
                text(
                    """
                    ALTER TABLE bot_state
                    ADD COLUMN fourmeme_trading_enabled BOOLEAN
                    DEFAULT 0
                    """
                )
            )


@asynccontextmanager
async def async_session_scope(
) -> AsyncIterator[AsyncSession]:

    async with SessionLocal() as session:

        yield session
