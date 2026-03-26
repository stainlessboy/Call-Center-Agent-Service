from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Base declarative class."""


engine = create_async_engine(
    settings.database_url,
    future=True,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_pool_max_overflow,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
