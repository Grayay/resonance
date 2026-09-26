from __future__ import annotations

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.db.models import Admin, User


async def upsert_telegram_user(session: AsyncSession, telegram_user: TelegramUser) -> User:
    user = await session.get(User, telegram_user.id)
    if user is None:
        user = User(
            telegram_user_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
        )
        session.add(user)
    else:
        user.username = telegram_user.username
        user.first_name = telegram_user.first_name
        user.last_name = telegram_user.last_name
    return user


async def ensure_user(
    session_factory: async_sessionmaker[AsyncSession], telegram_user: TelegramUser
) -> None:
    async with session_factory() as session, session.begin():
        await upsert_telegram_user(session, telegram_user)


async def is_admin(session: AsyncSession, user_id: int, superadmin_id: int) -> bool:
    if user_id == superadmin_id:
        return True
    return (
        await session.scalar(select(Admin.id).where(Admin.telegram_user_id == user_id))
    ) is not None


async def ensure_superadmin_user(session: AsyncSession, user_id: int) -> None:
    """Create a placeholder only if the superadmin has never opened the bot.

    PostgreSQL upsert is used in production; tests normally create the user explicitly.
    """
    if session.bind and session.bind.dialect.name == "postgresql":
        await session.execute(
            pg_insert(User)
            .values(telegram_user_id=user_id, first_name="Superadmin")
            .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
        )

