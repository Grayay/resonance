from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent

from resonance_bot.config import get_settings
from resonance_bot.db import build_engine, build_session_factory
from resonance_bot.handlers import admin_router, user_router
from resonance_bot.middleware import UserSyncMiddleware
from resonance_bot.scheduler import PersistentScheduler
from resonance_bot.services.notifications import NotificationService
from resonance_bot.services.registration import RegistrationService

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    registration_service = RegistrationService(
        session_factory, offer_ttl_minutes=settings.waitlist_offer_ttl_minutes
    )
    notifications = NotificationService(bot, session_factory, settings)
    scheduler = PersistentScheduler(
        bot,
        session_factory,
        settings,
        registration_service,
        notifications,
    )
    dispatcher = Dispatcher()
    middleware = UserSyncMiddleware(session_factory)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    @dispatcher.error()
    async def handle_error(event: ErrorEvent) -> bool:
        logger.exception("Unhandled update error", exc_info=event.exception)
        update = event.update
        try:
            if update.message:
                await update.message.answer("Что-то пошло не так. Попробуй ещё раз чуть позже.")
            elif update.callback_query:
                await update.callback_query.answer("Произошла ошибка", show_alert=True)
        except Exception:
            logger.exception("Could not report error to user")
        return True

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Меню участника"),
            BotCommand(command="admin", description="Панель администратора"),
        ]
    )
    scheduler_task = asyncio.create_task(scheduler.run(), name="persistent-scheduler")
    try:
        await dispatcher.start_polling(
            bot,
            settings=settings,
            session_factory=session_factory,
            registration_service=registration_service,
            notifications=notifications,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        scheduler.stop()
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()

