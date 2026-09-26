from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from resonance_bot.db.models import Feedback, Registration, User, WaitlistEntry

REGISTRATION_LABELS = {
    "registered": "записан",
    "cancelled": "отменил запись",
}
WAITLIST_LABELS = {
    "waiting": "ожидает",
    "offered": "место предложено",
    "accepted": "принял место",
    "declined": "отказался",
    "expired": "предложение истекло",
    "cancelled": "ожидание отменено",
}


async def event_csv(session: AsyncSession, event_id: int) -> bytes:
    registrations = list(
        (
            await session.execute(
                select(Registration, User, Feedback)
                .join(User, User.telegram_user_id == Registration.user_id)
                .outerjoin(Feedback, Feedback.registration_id == Registration.id)
                .where(Registration.event_id == event_id)
                .order_by(Registration.registered_at)
            )
        ).all()
    )
    registered_user_ids = {registration.user_id for registration, _, _ in registrations}
    waitlist = list(
        (
            await session.execute(
                select(WaitlistEntry, User)
                .join(User, User.telegram_user_id == WaitlistEntry.user_id)
                .where(WaitlistEntry.event_id == event_id)
                .order_by(WaitlistEntry.joined_at)
            )
        ).all()
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Telegram ID",
            "Имя пользователя",
            "Имя",
            "Дата регистрации",
            "Статус регистрации",
            "Подтвердил напоминание",
            "Дата отмены",
            "Статус листа ожидания",
            "Дата вступления в лист ожидания",
            "Оценка",
            "Комментарий",
            "Не присутствовал",
        ]
    )
    waitlist_by_user = {entry.user_id: entry for entry, _ in waitlist}
    for registration, user, feedback in registrations:
        entry = waitlist_by_user.get(user.telegram_user_id)
        writer.writerow(
            [
                user.telegram_user_id,
                user.username or "",
                " ".join(filter(None, [user.first_name, user.last_name])),
                registration.registered_at.isoformat(),
                REGISTRATION_LABELS[registration.status.value],
                "да" if registration.reminder_confirmed_at else "нет",
                registration.cancelled_at.isoformat() if registration.cancelled_at else "",
                WAITLIST_LABELS[entry.status.value] if entry else "",
                entry.joined_at.isoformat() if entry else "",
                feedback.rating if feedback and feedback.rating else "",
                feedback.comment if feedback and feedback.comment else "",
                "да" if feedback and feedback.did_not_attend else "нет" if feedback else "",
            ]
        )
    for entry, user in waitlist:
        if user.telegram_user_id in registered_user_ids:
            continue
        writer.writerow(
            [
                user.telegram_user_id,
                user.username or "",
                " ".join(filter(None, [user.first_name, user.last_name])),
                "",
                "",
                "",
                "",
                WAITLIST_LABELS[entry.status.value],
                entry.joined_at.isoformat(),
                "",
                "",
                "",
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")
