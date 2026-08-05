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


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
