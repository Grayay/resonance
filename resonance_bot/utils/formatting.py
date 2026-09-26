from __future__ import annotations

from html import escape

from resonance_bot.config import Settings
from resonance_bot.db.models import Event, EventStatus
from resonance_bot.utils.dates import human_date, human_datetime, human_time

STATUS_LABELS = {
    EventStatus.DRAFT: "Черновик",
    EventStatus.REGISTRATION_OPEN: "Регистрация открыта",
    EventStatus.REGISTRATION_CLOSED: "Регистрация закрыта",
    EventStatus.FINISHED: "Завершено",
    EventStatus.CANCELLED: "Отменено",
}


def event_deep_link(event_id: int, settings: Settings) -> str:
    return f"{settings.deep_link_base}?start=event_{event_id}"


def event_schedule_cta(event: Event, settings: Settings) -> str:
    return (
        "<blockquote>"
        f"<b>{human_date(event.starts_at, settings.tz)}</b>\n"
        f"сбор в <b>{human_time(event.starts_at, settings.tz)}</b>\n"
        f"{escape(event.venue_name)}\n"
        f"{escape(event.address)}"
        "</blockquote>\n\n"
        "<b>запись через кнопку ниже ↓</b>"
    )


def event_announcement(event: Event, settings: Settings) -> str:
    return (
        f"<b>{escape(event.title)}</b>\n\n"
        f"{escape(event.description.strip())}\n\n"
        f"{event_schedule_cta(event, settings)}"
    )


def event_card(
    event: Event, settings: Settings, *, active: int = 0, reserved: int = 0
) -> str:
    free = max(event.capacity - active, 0)
    if event.status == EventStatus.REGISTRATION_OPEN:
        if active + reserved >= event.capacity:
            availability = "Все доступные места заняты или предложены листу ожидания"
        else:
            availability = f"Осталось мест: <b>{free}</b>"
    else:
        availability = STATUS_LABELS[event.status]
    return (
        f"<b>{escape(event.title)}</b>\n\n"
        f"{escape(event.description)}\n\n"
        f"📅 {human_datetime(event.starts_at, settings.tz)}\n"
        f"📍 {escape(event.venue_name)}, {escape(event.address)}\n\n"
        f"{availability}"
    )


def registration_success(event: Event, settings: Settings) -> str:
    return (
        "<b>Записал тебя!</b>\n\n"
        f"{escape(event.title)}\n"
        f"{human_date(event.starts_at, settings.tz)}, начало в "
        f"{human_time(event.starts_at, settings.tz)}\n"
        f"{escape(event.venue_name)}\n"
        f"{escape(event.address)}\n\n"
        "Если планы поменяются — пожалуйста, нажми «Не смогу», и место перейдёт другому."
    )


def admin_event_card(event: Event, settings: Settings, *, active: int = 0) -> str:
    return (
        f"<b>{escape(event.title)}</b>\n"
        f"{human_datetime(event.starts_at, settings.tz)}\n"
        f"{escape(event.venue_name)}, {escape(event.address)}\n"
        f"Статус: <b>{STATUS_LABELS[event.status]}</b>\n"
        f"Записано: <b>{active}/{event.capacity}</b>"
    )
