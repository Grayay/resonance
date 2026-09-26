from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.db.models import (
    Event,
    EventStatus,
    JobStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    ReminderKind,
    WaitlistEntry,
    WaitlistStatus,
)


class RegistrationResult(StrEnum):
    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"
    FULL = "full"
    CLOSED = "closed"
    NOT_FOUND = "not_found"
    PAST = "past"


class WaitlistResult(StrEnum):
    JOINED = "joined"
    ALREADY_WAITING = "already_waiting"
    PLACE_AVAILABLE = "place_available"
    CLOSED = "closed"
    NOT_FOUND = "not_found"


class OfferResult(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_REGISTERED = "already_registered"
    EXPIRED = "expired"
    INVALID = "invalid"
    CLOSED = "closed"


@dataclass(slots=True)
class RegistrationOutcome:
    result: RegistrationResult
    registration_id: int | None = None


@dataclass(slots=True)
class WaitlistOutcome:
    result: WaitlistResult
    position: int | None = None


@dataclass(slots=True)
class Promotion:
    waitlist_entry_id: int
    event_id: int
    user_id: int
    expires_at: datetime


@dataclass(slots=True)
class CancelOutcome:
    cancelled: bool
    promotion: Promotion | None = None


@dataclass(slots=True)
class OfferOutcome:
    result: OfferResult
    registration_id: int | None = None
    promotion: Promotion | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RegistrationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        offer_ttl_minutes: int = 120,
    ) -> None:
        self.session_factory = session_factory
        self.offer_ttl = timedelta(minutes=offer_ttl_minutes)

    async def register(
        self, event_id: int, user_id: int, *, now: datetime | None = None
    ) -> RegistrationOutcome:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event = await self._lock_event(session, event_id)
            invalid = self._event_registration_error(event, now)
            if invalid:
                return RegistrationOutcome(invalid)

            registration = await session.scalar(
                select(Registration)
                .where(Registration.event_id == event_id, Registration.user_id == user_id)
                .with_for_update()
            )
            if registration and registration.status == RegistrationStatus.REGISTERED:
                return RegistrationOutcome(
                    RegistrationResult.ALREADY_REGISTERED, registration.id
                )

            active, reserved = await self._occupancy(session, event_id)
            if active + reserved >= event.capacity:
                return RegistrationOutcome(RegistrationResult.FULL)

            if registration is None:
                registration = Registration(
                    event_id=event_id,
                    user_id=user_id,
                    status=RegistrationStatus.REGISTERED,
                    registered_at=now,
                )
                session.add(registration)
                await session.flush()
            else:
                registration.status = RegistrationStatus.REGISTERED
                registration.registered_at = now
                registration.cancelled_at = None
                registration.reminder_confirmed_at = None

            waitlist = await session.scalar(
                select(WaitlistEntry)
                .where(WaitlistEntry.event_id == event_id, WaitlistEntry.user_id == user_id)
                .with_for_update()
            )
            if waitlist:
                waitlist.status = WaitlistStatus.ACCEPTED
                waitlist.responded_at = now
            await self._schedule_jobs(session, event, registration, now)
            return RegistrationOutcome(RegistrationResult.REGISTERED, registration.id)

    async def join_waitlist(
        self, event_id: int, user_id: int, *, now: datetime | None = None
    ) -> WaitlistOutcome:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event = await self._lock_event(session, event_id)
            if event is None:
                return WaitlistOutcome(WaitlistResult.NOT_FOUND)
            if event.status != EventStatus.REGISTRATION_OPEN or as_utc(event.starts_at) <= now:
                return WaitlistOutcome(WaitlistResult.CLOSED)

            existing_registration = await session.scalar(
                select(Registration.id).where(
                    Registration.event_id == event_id,
                    Registration.user_id == user_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                )
            )
            active, reserved = await self._occupancy(session, event_id)
            if existing_registration or active + reserved < event.capacity:
                return WaitlistOutcome(WaitlistResult.PLACE_AVAILABLE)

            entry = await session.scalar(
                select(WaitlistEntry)
                .where(WaitlistEntry.event_id == event_id, WaitlistEntry.user_id == user_id)
                .with_for_update()
            )
            if entry and entry.status in (WaitlistStatus.WAITING, WaitlistStatus.OFFERED):
                position = await self._waitlist_position(session, entry)
                return WaitlistOutcome(WaitlistResult.ALREADY_WAITING, position)
            if entry is None:
                entry = WaitlistEntry(event_id=event_id, user_id=user_id, joined_at=now)
                session.add(entry)
                await session.flush()
            else:
                entry.status = WaitlistStatus.WAITING
                entry.joined_at = now
                entry.offered_at = None
                entry.offer_expires_at = None
                entry.responded_at = None
            position = await self._waitlist_position(session, entry)
            return WaitlistOutcome(WaitlistResult.JOINED, position)

    async def cancel(
        self, event_id: int, user_id: int, *, now: datetime | None = None
    ) -> CancelOutcome:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event = await self._lock_event(session, event_id)
            if event is None:
                return CancelOutcome(False)
            registration = await session.scalar(
                select(Registration)
                .where(
                    Registration.event_id == event_id,
                    Registration.user_id == user_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                )
                .with_for_update()
            )
            if registration is None:
                return CancelOutcome(False)
            registration.status = RegistrationStatus.CANCELLED
            registration.cancelled_at = now
            await session.execute(
                update(Reminder)
                .where(
                    Reminder.registration_id == registration.id,
                    Reminder.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
                )
                .values(status=JobStatus.SKIPPED)
            )
            promotion = await self._promote_next(session, event, now)
            return CancelOutcome(True, promotion)

    async def accept_offer(
        self, entry_id: int, user_id: int, *, now: datetime | None = None
    ) -> OfferOutcome:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event_id = await session.scalar(
                select(WaitlistEntry.event_id).where(
                    WaitlistEntry.id == entry_id, WaitlistEntry.user_id == user_id
                )
            )
            if event_id is None:
                return OfferOutcome(OfferResult.INVALID)
            event = await self._lock_event(session, event_id)
            entry = await session.scalar(
                select(WaitlistEntry)
                .where(WaitlistEntry.id == entry_id, WaitlistEntry.user_id == user_id)
                .with_for_update()
            )
            if entry is None or entry.status != WaitlistStatus.OFFERED:
                return OfferOutcome(OfferResult.INVALID)
            if (
                event is None
                or event.status != EventStatus.REGISTRATION_OPEN
                or as_utc(event.starts_at) <= now
            ):
                entry.status = WaitlistStatus.CANCELLED
                entry.responded_at = now
                return OfferOutcome(OfferResult.CLOSED)
            if entry.offer_expires_at is None or as_utc(entry.offer_expires_at) <= now:
                entry.status = WaitlistStatus.EXPIRED
                entry.responded_at = now
                promotion = await self._promote_next(session, event, now)
                return OfferOutcome(OfferResult.EXPIRED, promotion=promotion)

            registration = await session.scalar(
                select(Registration)
                .where(
                    Registration.event_id == entry.event_id,
                    Registration.user_id == user_id,
                )
                .with_for_update()
            )
            if registration and registration.status == RegistrationStatus.REGISTERED:
                entry.status = WaitlistStatus.ACCEPTED
                entry.responded_at = now
                return OfferOutcome(OfferResult.ALREADY_REGISTERED, registration.id)
            active = await self._active_count(session, event.id)
            if active >= event.capacity:
                entry.status = WaitlistStatus.WAITING
                entry.offered_at = None
                entry.offer_expires_at = None
                return OfferOutcome(OfferResult.INVALID)
            if registration is None:
                registration = Registration(
                    event_id=event.id,
                    user_id=user_id,
                    status=RegistrationStatus.REGISTERED,
                    registered_at=now,
                )
                session.add(registration)
                await session.flush()
            else:
                registration.status = RegistrationStatus.REGISTERED
                registration.registered_at = now
                registration.cancelled_at = None
                registration.reminder_confirmed_at = None
            entry.status = WaitlistStatus.ACCEPTED
            entry.responded_at = now
            await self._schedule_jobs(session, event, registration, now)
            return OfferOutcome(OfferResult.ACCEPTED, registration.id)

    async def decline_offer(
        self, entry_id: int, user_id: int, *, now: datetime | None = None
    ) -> OfferOutcome:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event_id = await session.scalar(
                select(WaitlistEntry.event_id).where(
                    WaitlistEntry.id == entry_id, WaitlistEntry.user_id == user_id
                )
            )
            if event_id is None:
                return OfferOutcome(OfferResult.INVALID)
            event = await self._lock_event(session, event_id)
            entry = await session.scalar(
                select(WaitlistEntry)
                .where(WaitlistEntry.id == entry_id, WaitlistEntry.user_id == user_id)
                .with_for_update()
            )
            if entry is None or entry.status != WaitlistStatus.OFFERED:
                return OfferOutcome(OfferResult.INVALID)
            entry.status = WaitlistStatus.DECLINED
            entry.responded_at = now
            promotion = await self._promote_next(session, event, now) if event else None
            return OfferOutcome(OfferResult.INVALID, promotion=promotion)

    async def expire_offer(
        self, entry_id: int, *, now: datetime | None = None
    ) -> Promotion | None:
        now = now or utcnow()
        async with self.session_factory() as session, session.begin():
            event_id = await session.scalar(
                select(WaitlistEntry.event_id).where(WaitlistEntry.id == entry_id)
            )
            if event_id is None:
                return None
            event = await self._lock_event(session, event_id)
            entry = await session.scalar(
                select(WaitlistEntry).where(WaitlistEntry.id == entry_id).with_for_update()
            )
            if (
                entry is None
                or entry.status != WaitlistStatus.OFFERED
                or entry.offer_expires_at is None
                or as_utc(entry.offer_expires_at) > now
            ):
                return None
            entry.status = WaitlistStatus.EXPIRED
            entry.responded_at = now
            return await self._promote_next(session, event, now) if event else None

    async def _lock_event(self, session: AsyncSession, event_id: int) -> Event | None:
        return await session.scalar(select(Event).where(Event.id == event_id).with_for_update())

    def _event_registration_error(
        self, event: Event | None, now: datetime
    ) -> RegistrationResult | None:
        if event is None:
            return RegistrationResult.NOT_FOUND
        if as_utc(event.starts_at) <= now:
            return RegistrationResult.PAST
        if event.status != EventStatus.REGISTRATION_OPEN:
            return RegistrationResult.CLOSED
        return None

    async def _active_count(self, session: AsyncSession, event_id: int) -> int:
        return int(
            await session.scalar(
                select(func.count(Registration.id)).where(
                    Registration.event_id == event_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                )
            )
            or 0
        )

    async def _occupancy(self, session: AsyncSession, event_id: int) -> tuple[int, int]:
        active = await self._active_count(session, event_id)
        reserved = int(
            await session.scalar(
                select(func.count(WaitlistEntry.id)).where(
                    WaitlistEntry.event_id == event_id,
                    WaitlistEntry.status == WaitlistStatus.OFFERED,
                )
            )
            or 0
        )
        return active, reserved

    async def _waitlist_position(self, session: AsyncSession, entry: WaitlistEntry) -> int:
        ahead = int(
            await session.scalar(
                select(func.count(WaitlistEntry.id)).where(
                    WaitlistEntry.event_id == entry.event_id,
                    WaitlistEntry.status == WaitlistStatus.WAITING,
                    WaitlistEntry.joined_at < entry.joined_at,
                )
            )
            or 0
        )
        return ahead + 1

    async def _promote_next(
        self, session: AsyncSession, event: Event, now: datetime
    ) -> Promotion | None:
        if event.status != EventStatus.REGISTRATION_OPEN or as_utc(event.starts_at) <= now:
            return None
        active, reserved = await self._occupancy(session, event.id)
        if active + reserved >= event.capacity:
            return None
        entry = await session.scalar(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.event_id == event.id,
                WaitlistEntry.status == WaitlistStatus.WAITING,
            )
            .order_by(WaitlistEntry.joined_at, WaitlistEntry.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if entry is None:
            return None
        entry.status = WaitlistStatus.OFFERED
        entry.offered_at = now
        entry.offer_expires_at = now + self.offer_ttl
        await session.flush()
        return Promotion(entry.id, event.id, entry.user_id, entry.offer_expires_at)

    async def _schedule_jobs(
        self, session: AsyncSession, event: Event, registration: Registration, now: datetime
    ) -> None:
        await session.execute(
            update(Reminder)
            .where(
                Reminder.registration_id == registration.id,
                Reminder.status == JobStatus.PENDING,
            )
            .values(status=JobStatus.SKIPPED)
        )
        for offset in sorted(set(event.reminder_offsets_hours or []), reverse=True):
            scheduled_at = as_utc(event.starts_at) - timedelta(hours=offset)
            if scheduled_at > now:
                existing = await session.scalar(
                    select(Reminder).where(
                        Reminder.registration_id == registration.id,
                        Reminder.kind == ReminderKind.EVENT,
                        Reminder.offset_hours == offset,
                        Reminder.scheduled_at == scheduled_at,
                    )
                )
                if existing is None:
                    session.add(
                        Reminder(
                            registration_id=registration.id,
                            kind=ReminderKind.EVENT,
                            offset_hours=offset,
                            scheduled_at=scheduled_at,
                        )
                    )
                elif existing.status != JobStatus.SENT:
                    existing.status = JobStatus.PENDING
                    existing.last_error = None
        if event.feedback_at and as_utc(event.feedback_at) > now:
            existing = await session.scalar(
                select(Reminder).where(
                    Reminder.registration_id == registration.id,
                    Reminder.kind == ReminderKind.FEEDBACK,
                    Reminder.scheduled_at == as_utc(event.feedback_at),
                )
            )
            if existing is None:
                session.add(
                    Reminder(
                        registration_id=registration.id,
                        kind=ReminderKind.FEEDBACK,
                        offset_hours=None,
                        scheduled_at=as_utc(event.feedback_at),
                    )
                )
            elif existing.status != JobStatus.SENT:
                existing.status = JobStatus.PENDING
                existing.last_error = None
