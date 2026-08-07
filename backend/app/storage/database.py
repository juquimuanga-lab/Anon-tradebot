"""SQLAlchemy async engine/session setup (SQLite for MVP)."""
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config.settings import settings

Base = declarative_base()

db_path = settings.database_url.split("///")[-1]
if db_path and db_path not in (":memory:",):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    from app.storage import models  # noqa: F401 ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_add_missing_columns)


def _migrate_add_missing_columns(conn) -> None:
    """create_all only creates tables that don't exist yet - it never alters
    an existing table. This adds columns introduced after the table already
    existed on a deployed database (e.g. Railway's persistent volume).
    Safe to run on every startup: checks each column before adding it.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    if "orders" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("orders")}
    if "rule_id" not in existing:
        conn.execute(text("ALTER TABLE orders ADD COLUMN rule_id INTEGER"))
    if "owner_user_id" not in existing:
        conn.execute(text("ALTER TABLE orders ADD COLUMN owner_user_id INTEGER"))


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
