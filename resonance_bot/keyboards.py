from __future__ import annotations

from urllib.parse import quote

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from resonance_bot.config import Settings
from resonance_bot.db.models import Event, EventStatus
from resonance_bot.utils.formatting import event_deep_link


def user_menu(settings: Settings) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Записаться на событие")],
            [KeyboardButton(text="Мои записи")],
            [KeyboardButton(text="Написать организатору")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


def admin_menu(*, superadmin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Создать мероприятие")],
        [KeyboardButton(text="Мероприятия")],
        [KeyboardButton(text="Рассылка")],
    ]
    if superadmin:
        rows.append([KeyboardButton(text="Администраторы")])
    rows.append([KeyboardButton(text="Меню участника")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def contact_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Написать организатору", url=settings.contact_url)]
        ]
    )


def event_keyboard(
    event: Event,
    settings: Settings,
    *,
    registered: bool = False,
    full: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if event.map_url:
        builder.button(text="Открыть карты", url=event.map_url)
    if registered:
        link = quote(event_deep_link(event.id, settings), safe="")
        share_text = quote(f"Присоединяйся: {event.title}", safe="")
        builder.button(
            text="Позвать друга",
            url=f"https://t.me/share/url?url={link}&text={share_text}",
        )
        builder.button(text="Не смогу", callback_data=f"cancel:ask:{event.id}")
    elif event.status == EventStatus.REGISTRATION_OPEN:
        if full:
            builder.button(text="В лист ожидания", callback_data=f"wait:join:{event.id}")
        else:
            builder.button(text="Записаться", callback_data=f"event:register:{event.id}")
    builder.adjust(1)
    return builder.as_markup()


def cancel_confirm_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить запись", callback_data=f"cancel:yes:{event_id}"
                )
            ],
            [InlineKeyboardButton(text="Оставить запись", callback_data=f"cancel:no:{event_id}")],
        ]
    )


def offer_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Забрать место", callback_data=f"offer:accept:{entry_id}")],
            [InlineKeyboardButton(text="Не смогу", callback_data=f"offer:decline:{entry_id}")],
        ]
    )


def reminder_keyboard(event: Event) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Буду", callback_data=f"reminder:confirm:{event.id}")],
            [InlineKeyboardButton(text="Не смогу", callback_data=f"cancel:ask:{event.id}")],
        ]
    )


def day_reminder_keyboard(event: Event) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть карты", url=event.map_url)],
            [InlineKeyboardButton(text="Не смогу", callback_data=f"cancel:ask:{event.id}")],
        ]
    )


def feedback_keyboard(registration_id: int) -> InlineKeyboardMarkup:
    ratings = [
        InlineKeyboardButton(
            text=str(value), callback_data=f"feedback:rate:{registration_id}:{value}"
        )
        for value in range(1, 6)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            ratings,
            [
                InlineKeyboardButton(
                    text="К сожалению, не получилось прийти",
                    callback_data=f"feedback:absent:{registration_id}",
                )
            ],
        ]
    )


def admin_event_keyboard(event: Event) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Участники", callback_data=f"admin:event:{event.id}:participants")
    builder.button(text="Рассылка участникам", callback_data=f"admin:event:{event.id}:broadcast")
    builder.button(text="Редактировать", callback_data=f"admin:event:{event.id}:edit")
    if event.status == EventStatus.REGISTRATION_OPEN:
        builder.button(text="Закрыть запись", callback_data=f"admin:event:{event.id}:close")
    elif event.status in (EventStatus.DRAFT, EventStatus.REGISTRATION_CLOSED) and (
        event.published_at is None
    ):
        builder.button(text="Опубликовать", callback_data=f"admin:event:{event.id}:publish")
    builder.button(text="Ссылка для публикации", callback_data=f"admin:event:{event.id}:manual")
    builder.button(text="Экспорт CSV", callback_data=f"admin:event:{event.id}:export")
    builder.button(text="Статистика", callback_data=f"admin:event:{event.id}:stats")
    if event.status != EventStatus.CANCELLED:
        builder.button(text="Отменить мероприятие", callback_data=f"admin:event:{event.id}:cancel")
    builder.adjust(2, 1, 1, 1, 2, 1)
    return builder.as_markup()
