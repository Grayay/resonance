from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    BroadcastLog,
    ChannelPublication,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
)
from resonance_bot.keyboards import offer_keyboard
from resonance_bot.services.registration import Promotion
from resonance_bot.utils.formatting import (
    event_announcement,
    event_deep_link,
    event_schedule_cta,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliverySummary:
    total: int
    delivered: int
    failed: int


class NotificationService:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.settings = settings

    async def publish_event(self, event_id: int) -> ChannelPublication:
        async with self.session_factory() as session:
            event = await session.get(Event, event_id, with_for_update=True)
            if event is None:
                raise ValueError("Мероприятие не найдено")
            if event.status == EventStatus.CANCELLED:
                raise ValueError("Отменённое мероприятие нельзя опубликовать")
            starts_at = (
                event.starts_at.replace(tzinfo=UTC)
                if event.starts_at.tzinfo is None
                else event.starts_at.astimezone(UTC)
            )
            if starts_at <= datetime.now(UTC):
                raise ValueError("Нельзя опубликовать прошедшее мероприятие")
            if event.published_at is not None:
                raise ValueError("Мероприятие уже было опубликовано")
            text = event_announcement(event, self.settings)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Записаться",
                            url=event_deep_link(event.id, self.settings),
                        )
                    ]
                ]
            )
            photo_message_id: int | None = None
            text_message_id: int | None = None
            if event.cover_file_id and len(text) <= 900:
                sent = await self.bot.send_photo(
                    self.settings.channel_id,
                    event.cover_file_id,
                    caption=text,
                    reply_markup=keyboard,
                )
                photo_message_id = sent.message_id
            elif len(text) <= 4000 and event.cover_file_id:
                photo = await self.bot.send_photo(self.settings.channel_id, event.cover_file_id)
                message = await self.bot.send_message(
                    self.settings.channel_id, text, reply_markup=keyboard
                )
                photo_message_id = photo.message_id
                text_message_id = message.message_id
            elif len(text) <= 4000:
                message = await self.bot.send_message(
                    self.settings.channel_id, text, reply_markup=keyboard
                )
                text_message_id = message.message_id
            else:
                if event.cover_file_id:
                    photo = await self.bot.send_photo(
                        self.settings.channel_id, event.cover_file_id
                    )
                    photo_message_id = photo.message_id
                await self.bot.send_message(
                    self.settings.channel_id,
                    f"<b>{escape(event.title)}</b>\n\n{escape(event.description)}",
                )
                message = await self.bot.send_message(
                    self.settings.channel_id,
                    event_schedule_cta(event, self.settings),
                    reply_markup=keyboard,
                )
                text_message_id = message.message_id

            publication = ChannelPublication(
                event_id=event.id,
                channel_id=self.settings.channel_id,
                photo_message_id=photo_message_id,
                text_message_id=text_message_id,
            )
            session.add(publication)
            event.status = EventStatus.REGISTRATION_OPEN
            event.published_at = datetime.now(UTC)
            await session.commit()
            return publication

    async def disable_channel_registration(self, event_id: int) -> None:
        async with self.session_factory() as session:
            publications = list(
                await session.scalars(
                    select(ChannelPublication).where(ChannelPublication.event_id == event_id)
                )
            )
        for publication in publications:
            message_id = publication.text_message_id or publication.photo_message_id
            if not message_id:
                continue
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=publication.channel_id,
                    message_id=message_id,
                    reply_markup=None,
                )
            except TelegramAPIError:
                logger.warning("Could not remove channel button", exc_info=True)

    async def send_promotion(self, promotion: Promotion) -> bool:
        async with self.session_factory() as session:
            event = await session.get(Event, promotion.event_id)
        if event is None:
            return False
        try:
            await self.bot.send_message(
                promotion.user_id,
                f"Освободилось место на мероприятие <b>{escape(event.title)}</b>!\n\n"
                "Забери его кнопкой ниже. Предложение действует ограниченное время.",
                reply_markup=offer_keyboard(promotion.waitlist_entry_id),
            )
            return True
        except TelegramAPIError:
            logger.info("Could not deliver waitlist offer to %s", promotion.user_id)
            return False

    async def event_participant_ids(self, event_id: int) -> list[int]:
        async with self.session_factory() as session:
            return list(
                await session.scalars(
                    select(Registration.user_id).where(
                        Registration.event_id == event_id,
                        Registration.status == RegistrationStatus.REGISTERED,
                    )
                )
            )

    async def all_user_ids(self) -> list[int]:
        async with self.session_factory() as session:
            return list(await session.scalars(select(User.telegram_user_id)))

    async def broadcast(
        self,
        user_ids: list[int],
        text: str,
        *,
        created_by: int,
        event_id: int | None = None,
    ) -> DeliverySummary:
        delivered = 0
        failed = 0
        for index, user_id in enumerate(user_ids, start=1):
            try:
                await self.bot.send_message(user_id, text)
                delivered += 1
            except TelegramAPIError:
                failed += 1
                logger.info("Broadcast delivery failed for %s", user_id)
            if index % 25 == 0:
                await asyncio.sleep(1)
        summary = DeliverySummary(len(user_ids), delivered, failed)
        async with self.session_factory() as session, session.begin():
            session.add(
                BroadcastLog(
                    event_id=event_id,
                    created_by=created_by,
                    text=text,
                    total=summary.total,
                    delivered=summary.delivered,
                    failed=summary.failed,
                )
            )
        return summary
