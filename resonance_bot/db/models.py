from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resonance_bot.db.base import Base


class EventStatus(StrEnum):
    DRAFT = "draft"
    REGISTRATION_OPEN = "registration_open"
    REGISTRATION_CLOSED = "registration_closed"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class RegistrationStatus(StrEnum):
    REGISTERED = "registered"
    CANCELLED = "cancelled"


class WaitlistStatus(StrEnum):
    WAITING = "waiting"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ReminderKind(StrEnum):
    EVENT = "event"
    FEEDBACK = "feedback"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    registrations: Mapped[list[Registration]] = relationship(back_populates="user")


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), unique=True
    )
    added_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    venue_name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str] = mapped_column(String(500))
    map_url: Mapped[str] = mapped_column(String(1000))
    cover_file_id: Mapped[str | None] = mapped_column(String(512))
    capacity: Mapped[int] = mapped_column(Integer)
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status", native_enum=False),
        default=EventStatus.DRAFT,
        index=True,
    )
    reminder_offsets_hours: Mapped[list[int]] = mapped_column(JSON, default=list)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    registrations: Mapped[list[Registration]] = relationship(back_populates="event")

    __table_args__ = (
        Index("ix_events_upcoming", "status", "starts_at"),
    )


class Registration(Base):
    __tablename__ = "registrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[RegistrationStatus] = mapped_column(
        Enum(RegistrationStatus, name="registration_status", native_enum=False),
        default=RegistrationStatus.REGISTERED,
        index=True,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="registrations")
    user: Mapped[User] = relationship(back_populates="registrations")
    feedback: Mapped[Feedback | None] = relationship(back_populates="registration", uselist=False)

    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_registration_event_user"),)


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_user_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[WaitlistStatus] = mapped_column(
        Enum(WaitlistStatus, name="waitlist_status", native_enum=False),
        default=WaitlistStatus.WAITING,
        index=True,
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_waitlist_event_user"),
        Index("ix_waitlist_fifo", "event_id", "status", "joined_at"),
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration_id: Mapped[int] = mapped_column(
        ForeignKey("registrations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ReminderKind] = mapped_column(
        Enum(ReminderKind, name="reminder_kind", native_enum=False)
    )
    offset_hours: Mapped[int | None] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=False), default=JobStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "kind",
            "offset_hours",
            "scheduled_at",
            name="uq_reminder_registration_schedule",
        ),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    registration_id: Mapped[int] = mapped_column(
        ForeignKey("registrations.id", ondelete="CASCADE"), unique=True
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    did_not_attend: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    registration: Mapped[Registration] = relationship(back_populates="feedback")


class ChannelPublication(Base):
    __tablename__ = "channel_publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(String(255))
    photo_message_id: Mapped[int | None] = mapped_column(BigInteger)
    text_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BroadcastLog(Base):
    __tablename__ = "broadcast_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    created_by: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer)
    delivered: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Keeps pyright/mypy-friendly imports available to consumers of this module.
ModelData = dict[str, Any]
