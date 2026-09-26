from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from resonance_bot.db.base import Base
from resonance_bot.db.models import Event, EventStatus, User
from resonance_bot.services.registration import RegistrationService


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add_all(
            [
                User(telegram_user_id=user_id, first_name=f"User {user_id}")
                for user_id in range(1, 6)
            ]
        )
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def event_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session, session.begin():
        event = Event(
            title="Тестовое событие",
            description="Описание",
            starts_at=datetime.now(UTC) + timedelta(days=7),
            venue_name="Площадка",
            address="Адрес",
            map_url="https://example.com/map",
            capacity=1,
            status=EventStatus.REGISTRATION_OPEN,
            reminder_offsets_hours=[],
            created_by=1,
        )
        session.add(event)
        await session.flush()
        return event.id


@pytest_asyncio.fixture
async def service(
    session_factory: async_sessionmaker[AsyncSession],
) -> RegistrationService:
    return RegistrationService(session_factory, offer_ttl_minutes=120)

