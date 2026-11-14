from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.db.models import (
    Event,
    EventStatus,
    JobStatus,
    PaymentReceipt,
    PaymentStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    ReminderKind,
    User,
)

PAYMENT_INSTRUCTION = "После оплаты, пришлите, пожалуйста, скрин в этот чат."
RECEIPT_CONFIRMATION = (
    "Спасибо! Чек получен, вы записаны.\n\n"
    "Если вы отмените запись, администратор вернет вам денежные средства."
)
REFUND_MESSAGE = "Запись отменена. Администратор вернет вам денежные средства."


def positive_price(raw: str) -> int:
    value = raw.strip()
    if not value.isdecimal() or not 1 <= int(value) <= 2_147_483_647:
        raise ValueError("Price must be a positive 32-bit integer")
    return int(value)


def payment_request(event: Event, amount: int, *, reminder: bool = False) -> str:
    from html import escape

    intro = "Напоминаем об оплате мероприятия" if reminder else "Пожалуйста, оплатите мероприятие"
    return f"{intro} «{escape(event.title)}» — {amount} ₽.\n\n{PAYMENT_INSTRUCTION}"


def pending_conditions(user_id: int, now: datetime):
    return (
        Registration.user_id == user_id,
        Registration.status == RegistrationStatus.REGISTERED,
        Registration.payment_status == PaymentStatus.PENDING,
        Event.is_paid.is_(True),
        Event.starts_at > now,
        Event.status.in_([EventStatus.REGISTRATION_OPEN, EventStatus.REGISTRATION_CLOSED]),
    )


async def pending_payments(session: AsyncSession, user_id: int):
    return list(
        (
            await session.execute(
                select(Registration, Event)
                .join(Event, Registration.event_id == Event.id)
                .where(*pending_conditions(user_id, datetime.now(UTC)))
                .order_by(Event.starts_at, Registration.id)
            )
        ).all()
    )


async def attach_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    registration_id: int,
    user_id: int,
    file_id: str,
    *,
    file_unique_id: str | None = None,
    expected_cycle: int | None = None,
) -> bool:
    async with session_factory() as session, session.begin():
        event_id = await session.scalar(
            select(Registration.event_id).where(
                Registration.id == registration_id,
                Registration.user_id == user_id,
            )
        )
        if event_id is None:
            return False
        # Match the event -> registration lock order used by registration/cancellation.
        await session.scalar(select(Event).where(Event.id == event_id).with_for_update())
        # Serialize receipt uploads for this user, including uploads for different events.
        await session.scalar(
            select(User)
            .where(
                User.telegram_user_id == user_id,
            )
            .with_for_update()
        )
        registration = await session.scalar(
            select(Registration)
            .join(Event, Registration.event_id == Event.id)
            .where(
                Registration.id == registration_id, *pending_conditions(user_id, datetime.now(UTC))
            )
            .with_for_update(of=Registration)
        )
        if (
            registration is None
            or (expected_cycle is not None and registration.payment_cycle != expected_cycle)
            or await receipt_used(session, user_id, file_id, file_unique_id)
        ):
            return False
        session.add(
            PaymentReceipt(
                registration_id=registration.id,
                payment_cycle=registration.payment_cycle,
                amount=registration.payment_amount,
                file_id=file_id,
                file_unique_id=file_unique_id,
                received_at=datetime.now(UTC),
            )
        )
        registration.payment_status = PaymentStatus.RECEIVED
        await session.execute(
            update(Reminder)
            .where(
                Reminder.registration_id == registration_id,
                Reminder.kind == ReminderKind.PAYMENT,
                Reminder.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
            )
            .values(status=JobStatus.SKIPPED)
        )
        return True


async def receipt_used(
    session: AsyncSession,
    user_id: int,
    file_id: str,
    file_unique_id: str | None = None,
) -> bool:
    matches = [PaymentReceipt.file_id == file_id]
    if file_unique_id:
        matches.append(PaymentReceipt.file_unique_id == file_unique_id)
    return (
        await session.scalar(
            select(PaymentReceipt.id)
            .join(Registration, PaymentReceipt.registration_id == Registration.id)
            .where(
                Registration.user_id == user_id,
                or_(*matches),
            )
            .limit(1)
        )
        is not None
    )


async def payment_counts(session: AsyncSession, event_id: int) -> tuple[int, int]:
    counts = dict(
        (
            await session.execute(
                select(Registration.payment_status, func.count(Registration.id))
                .where(
                    Registration.event_id == event_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                )
                .group_by(Registration.payment_status)
            )
        ).all()
    )
    return counts.get(PaymentStatus.RECEIVED, 0), counts.get(PaymentStatus.PENDING, 0)


async def payment_participants(
    session: AsyncSession,
    event_id: int,
    status: PaymentStatus | None = None,
    *,
    page: int | None = None,
):
    query = (
        select(Registration, User)
        .join(User, User.telegram_user_id == Registration.user_id)
        .where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.REGISTERED,
        )
    )
    if status is not None:
        query = query.where(Registration.payment_status == status)
    query = query.order_by(Registration.id)
    if page is not None:
        query = query.offset(page * 6).limit(7)
    return list((await session.execute(query)).all())


async def event_receipts(session: AsyncSession, event_id: int, page: int):
    query = (
        select(PaymentReceipt, User)
        .join(Registration, PaymentReceipt.registration_id == Registration.id)
        .join(User, User.telegram_user_id == Registration.user_id)
        .where(Registration.event_id == event_id)
        .order_by(PaymentReceipt.id)
    )
    return list((await session.execute(query.offset(page).limit(2))).all())
