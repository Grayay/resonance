from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.services.users import upsert_telegram_user


class UserSyncMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_user = event.from_user
        if telegram_user:
            async with self.session_factory() as session, session.begin():
                await upsert_telegram_user(session, telegram_user)
        return await handler(event, data)

