from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    Event,
    EventStatus,
    JobStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    ReminderKind,
    User,
    WaitlistEntry,
    WaitlistStatus,
)
from resonance_bot.keyboards import day_reminder_keyboard, feedback_keyboard, reminder_keyboard
from resonance_bot.services.notifications import NotificationService
from resonance_bot.services.registration import RegistrationService
from resonance_bot.utils.dates import human_date, human_time

logger = logging.getLogger(__name__)


class PersistentScheduler:
    """A database-backed at-least-once worker for reminders and waitlist expirations."""

    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        registration_service: RegistrationService,
        notifications: NotificationService,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.settings = settings
        self.registration_service = registration_service
        self.notifications = notifications
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Persistent scheduler started")
        while not self._stop.is_set():
            try:
                await self._recover_stale_jobs()
                await self._process_due_reminders()
                await self._process_expired_offers()
                await self._finish_past_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scheduler_poll_seconds
                )
            except TimeoutError:
                pass
        logger.info("Persistent scheduler stopped")

    async def _recover_stale_jobs(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(Reminder)
                .where(Reminder.status == JobStatus.PROCESSING, Reminder.locked_at < cutoff)
                .values(status=JobStatus.PENDING, locked_at=None)
            )

    async def _claim_job(self) -> int | None:
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            job = await session.scalar(
                select(Reminder)
                .where(Reminder.status == JobStatus.PENDING, Reminder.scheduled_at <= now)
                .order_by(Reminder.scheduled_at, Reminder.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = JobStatus.PROCESSING
            job.locked_at = now
            job.attempts += 1
            return job.id

    async def _process_due_reminders(self) -> None:
        for _ in range(100):
            job_id = await self._claim_job()
            if job_id is None:
                break
            await self._deliver_job(job_id)

    async def _deliver_job(self, job_id: int) -> None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Reminder, Registration, Event, User)
                    .join(Registration, Reminder.registration_id == Registration.id)
                    .join(Event, Registration.event_id == Event.id)
                    .join(User, Registration.user_id == User.telegram_user_id)
                    .where(Reminder.id == job_id)
                )
            ).one_or_none()
        if row is None:
            return
        job, registration, event, user = row
        if (
            registration.status != RegistrationStatus.REGISTERED
            or event.status == EventStatus.CANCELLED
        ):
            await self._mark_job(job_id, JobStatus.SKIPPED)
            return
        try:
            if job.kind == ReminderKind.FEEDBACK:
                await self.bot.send_message(
                    user.telegram_user_id,
                    f"Привет, {escape(user.first_name)}!\n"
                    "Подскажи, как тебе сегодняшнее мероприятие?",
                    reply_markup=feedback_keyboard(registration.id),
                )
            elif (job.offset_hours or 0) >= 12:
                await self.bot.send_message(
                    user.telegram_user_id,
                    f"{escape(user.first_name)}, привет! Уже скоро встречаемся:\n\n"
                    f"<b>{escape(event.title)}</b>\n"
                    f"{human_date(event.starts_at, self.settings.tz)}, "
                    f"{human_time(event.starts_at, self.settings.tz)}\n"
                    f"{escape(event.venue_name)}, {escape(event.address)}\n\n"
                    "Если планы поменялись — выбери «Не смогу».",
                    reply_markup=reminder_keyboard(event),
                )
            else:
                await self.bot.send_message(
                    user.telegram_user_id,
                    "<b>Уже сегодня!</b>\n\n"
                    f"{escape(event.title)}, начало в "
                    f"{human_time(event.starts_at, self.settings.tz)}\n"
                    f"{escape(event.venue_name)}, {escape(event.address)}\n\n"
                    f"Если потеряешься — пиши {escape(self.settings.community_contact)}, "
                    "найдём тебя.",
                    reply_markup=day_reminder_keyboard(event),
                )
            await self._mark_job(job_id, JobStatus.SENT)
        except TelegramAPIError as exc:
            logger.info("Reminder %s failed: %s", job_id, exc)
            status = JobStatus.FAILED if job.attempts >= 3 else JobStatus.PENDING
            await self._mark_job(job_id, status, str(exc))

    async def _mark_job(
        self, job_id: int, status: JobStatus, error: str | None = None
    ) -> None:
        values: dict[str, object] = {"status": status, "locked_at": None, "last_error": error}
        if status == JobStatus.SENT:
            values["sent_at"] = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            await session.execute(update(Reminder).where(Reminder.id == job_id).values(**values))

    async def _process_expired_offers(self) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            ids = list(
                await session.scalars(
                    select(WaitlistEntry.id)
                    .where(
                        WaitlistEntry.status == WaitlistStatus.OFFERED,
                        WaitlistEntry.offer_expires_at <= now,
                    )
                    .order_by(WaitlistEntry.offer_expires_at)
                    .limit(100)
                )
            )
        for entry_id in ids:
            promotion = await self.registration_service.expire_offer(entry_id, now=now)
            if promotion:
                await self.notifications.send_promotion(promotion)

    async def _finish_past_events(self) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(Event)
                .where(
                    Event.starts_at < now,
                    Event.status.in_([
                        EventStatus.REGISTRATION_OPEN,
                        EventStatus.REGISTRATION_CLOSED,
                    ]),
                )
                .values(status=EventStatus.FINISHED)
            )

