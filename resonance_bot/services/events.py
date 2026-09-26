from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from resonance_bot.db.models import (
    Event,
    Feedback,
    JobStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    ReminderKind,
    WaitlistEntry,
    WaitlistStatus,
)


@dataclass(slots=True)
class EventStats:
    capacity: int
    active: int
    free: int
    waitlist: int
    cancellations: int
    confirmations: int
    feedback_count: int
    average_rating: float | None


async def event_stats(session: AsyncSession, event: Event) -> EventStats:
    active = int(
        await session.scalar(
            select(func.count(Registration.id)).where(
                Registration.event_id == event.id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
        or 0
    )
    cancellations = int(
        await session.scalar(
            select(func.count(Registration.id)).where(
                Registration.event_id == event.id,
                Registration.status == RegistrationStatus.CANCELLED,
            )
        )
        or 0
    )
    confirmations = int(
        await session.scalar(
            select(func.count(Registration.id)).where(
                Registration.event_id == event.id,
                Registration.reminder_confirmed_at.is_not(None),
            )
        )
        or 0
    )
    waitlist = int(
        await session.scalar(
            select(func.count(WaitlistEntry.id)).where(
                WaitlistEntry.event_id == event.id,
                WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
            )
        )
        or 0
    )
    feedback_row = (
        await session.execute(
            select(func.count(Feedback.id), func.avg(Feedback.rating))
            .join(Registration, Feedback.registration_id == Registration.id)
            .where(Registration.event_id == event.id)
        )
    ).one()
    return EventStats(
        capacity=event.capacity,
        active=active,
        free=max(event.capacity - active, 0),
        waitlist=waitlist,
        cancellations=cancellations,
        confirmations=confirmations,
        feedback_count=int(feedback_row[0] or 0),
        average_rating=float(feedback_row[1]) if feedback_row[1] is not None else None,
    )


async def reschedule_registration_jobs(
    session: AsyncSession, event: Event, *, now: datetime | None = None
) -> None:
    now = now or datetime.now(UTC)
    registration_ids = list(
        await session.scalars(
            select(Registration.id).where(
                Registration.event_id == event.id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
    )
    if not registration_ids:
        return
    await session.execute(
        update(Reminder)
        .where(Reminder.registration_id.in_(registration_ids), Reminder.status == JobStatus.PENDING)
        .values(status=JobStatus.SKIPPED)
    )
    for registration_id in registration_ids:
        for offset in sorted(set(event.reminder_offsets_hours or []), reverse=True):
            scheduled_at = event.starts_at - timedelta(hours=offset)
            if scheduled_at > now:
                existing = await session.scalar(
                    select(Reminder).where(
                        Reminder.registration_id == registration_id,
                        Reminder.kind == ReminderKind.EVENT,
                        Reminder.offset_hours == offset,
                        Reminder.scheduled_at == scheduled_at,
                    )
                )
                if existing is None:
                    session.add(
                        Reminder(
                            registration_id=registration_id,
                            kind=ReminderKind.EVENT,
                            offset_hours=offset,
                            scheduled_at=scheduled_at,
                        )
                    )
                elif existing.status != JobStatus.SENT:
                    existing.status = JobStatus.PENDING
                    existing.last_error = None
        if event.feedback_at and event.feedback_at > now:
            existing = await session.scalar(
                select(Reminder).where(
                    Reminder.registration_id == registration_id,
                    Reminder.kind == ReminderKind.FEEDBACK,
                    Reminder.scheduled_at == event.feedback_at,
                )
            )
            if existing is None:
                session.add(
                    Reminder(
                        registration_id=registration_id,
                        kind=ReminderKind.FEEDBACK,
                        offset_hours=None,
                        scheduled_at=event.feedback_at,
                    )
                )
            elif existing.status != JobStatus.SENT:
                existing.status = JobStatus.PENDING
                existing.last_error = None
