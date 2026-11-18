from __future__ import annotations

from html import escape

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    Event,
    EventStatus,
    Feedback,
    PaymentStatus,
    Registration,
    RegistrationStatus,
    User,
)
from resonance_bot.utils.dates import human_date, human_datetime, human_time

STATUS_LABELS = {
    EventStatus.DRAFT: "Черновик",
    EventStatus.REGISTRATION_OPEN: "Регистрация открыта",
    EventStatus.REGISTRATION_CLOSED: "Регистрация закрыта",
    EventStatus.FINISHED: "Завершено",
    EventStatus.CANCELLED: "Отменено",
}

CHANNEL_LABELS = {"main": "Основной", "test": "Тестовый"}


def event_deep_link(event_id: int, settings: Settings) -> str:
    return f"{settings.deep_link_base}?start=event_{event_id}"


def event_schedule_cta(event: Event, settings: Settings) -> str:
    return (
        (f"🎟 билет = {event.price} ₽\n\n" if event.is_paid else "") + "<blockquote>"
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
            availability = "Мест нет"
        elif free >= 6:
            availability = "Места есть"
        else:
            availability = "Остались последние места"
    else:
        availability = STATUS_LABELS[event.status]
    return (
        f"<b>{escape(event.title)}</b>\n\n"
        f"{escape(event.description)}\n\n"
        + (f"🎟 билет = {event.price} ₽\n\n" if event.is_paid else "")
        + f"📅 {human_datetime(event.starts_at, settings.tz)}\n"
        f"📍 {escape(event.venue_name)}, {escape(event.address)}\n\n"
        f"{availability}"
    )


def registration_success(
    event: Event,
    settings: Settings,
    *,
    payment_pending: bool = True,
    payment_amount: int | None = None,
) -> str:
    if event.is_paid and payment_pending:
        from resonance_bot.services.payments import payment_request

        if payment_amount is None:
            raise ValueError("A paid registration must have a payment amount")
        return "<b>Место за вами!</b>\n\n" + payment_request(event, payment_amount)
    return (
        "<b>Записал тебя!</b>\n\n"
        f"{escape(event.title)}\n"
        f"{human_date(event.starts_at, settings.tz)}, начало в "
        f"{human_time(event.starts_at, settings.tz)}\n"
        f"{escape(event.venue_name)}\n"
        f"{escape(event.address)}\n\n"
        "Если планы поменяются — пожалуйста, нажми «Не смогу», и место перейдёт другому."
    )


def admin_event_card(
    event: Event, settings: Settings, *, active: int = 0, paid: int = 0, pending: int = 0
) -> str:
    return (
        f"<b>{escape(event.title)}</b>\n"
        f"{human_datetime(event.starts_at, settings.tz)}\n"
        f"{escape(event.venue_name)}, {escape(event.address)}\n"
        f"Статус: <b>{STATUS_LABELS[event.status]}</b>\n"
        f"Канал: {CHANNEL_LABELS[event.publication_target]}\n"
        f"Записано: <b>{active}/{event.capacity}</b>"
        + (
            f"\nЦена: {event.price} ₽\nОплатили: {paid} · Ожидают оплату: {pending}"
            if event.is_paid
            else ""
        )
    )


def participant_line(
    registration: Registration,
    user: User,
    *,
    paid: bool,
    feedback: Feedback | None = None,
) -> str:
    status = "записан" if registration.status == RegistrationStatus.REGISTERED else "отменил"
    if paid and registration.status == RegistrationStatus.REGISTERED:
        status = (
            "✅ оплачено"
            if registration.payment_status == PaymentStatus.RECEIVED
            else "⏳ ожидает оплату"
        ) + f" · {registration.payment_amount} ₽"
    flags = []
    if registration.reminder_confirmed_at:
        flags.append("подтвердил")
    if feedback:
        flags.append("оставил отзыв")
    username = f"@{escape(user.username)}" if user.username else f"ID {user.telegram_user_id}"
    name = escape(" ".join(filter(None, [user.first_name, user.last_name])))
    suffix = f"; {', '.join(flags)}" if flags else ""
    return f"• {name} ({username}) — {status}{suffix}"
