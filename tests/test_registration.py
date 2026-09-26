from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from resonance_bot.db.models import (
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    WaitlistEntry,
    WaitlistStatus,
)
from resonance_bot.services.registration import (
    OfferResult,
    RegistrationResult,
    RegistrationService,
    WaitlistResult,
)


async def test_duplicate_registration_is_idempotent(
    service: RegistrationService, session_factory, event_id: int
) -> None:
    first = await service.register(event_id, 1)
    second = await service.register(event_id, 1)

    assert first.result == RegistrationResult.REGISTERED
    assert second.result == RegistrationResult.ALREADY_REGISTERED
    assert first.registration_id == second.registration_id
    async with session_factory() as session:
        count = await session.scalar(select(func.count(Registration.id)))
    assert count == 1


async def test_capacity_prevents_overbooking(
    service: RegistrationService, session_factory, event_id: int
) -> None:
    assert (await service.register(event_id, 1)).result == RegistrationResult.REGISTERED
    assert (await service.register(event_id, 2)).result == RegistrationResult.FULL

    async with session_factory() as session:
        active = await session.scalar(
            select(func.count(Registration.id)).where(
                Registration.status == RegistrationStatus.REGISTERED
            )
        )
    assert active == 1


async def test_cancellation_promotes_fifo_waitlist(
    service: RegistrationService, session_factory, event_id: int
) -> None:
    await service.register(event_id, 1)
    assert (await service.join_waitlist(event_id, 2)).result == WaitlistResult.JOINED
    assert (await service.join_waitlist(event_id, 3)).result == WaitlistResult.JOINED

    cancellation = await service.cancel(event_id, 1)
    assert cancellation.cancelled is True
    assert cancellation.promotion is not None
    assert cancellation.promotion.user_id == 2

    accepted = await service.accept_offer(cancellation.promotion.waitlist_entry_id, 2)
    assert accepted.result == OfferResult.ACCEPTED
    async with session_factory() as session:
        active_user = await session.scalar(
            select(Registration.user_id).where(
                Registration.event_id == event_id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
        third = await session.scalar(
            select(WaitlistEntry).where(
                WaitlistEntry.event_id == event_id, WaitlistEntry.user_id == 3
            )
        )
    assert active_user == 2
    assert third.status == WaitlistStatus.WAITING


async def test_declined_offer_moves_to_next_person(
    service: RegistrationService, event_id: int
) -> None:
    await service.register(event_id, 1)
    await service.join_waitlist(event_id, 2)
    await service.join_waitlist(event_id, 3)
    first = await service.cancel(event_id, 1)

    declined = await service.decline_offer(first.promotion.waitlist_entry_id, 2)
    assert declined.promotion is not None
    assert declined.promotion.user_id == 3


async def test_expired_offer_moves_to_next_person(
    service: RegistrationService, event_id: int
) -> None:
    await service.register(event_id, 1)
    await service.join_waitlist(event_id, 2)
    await service.join_waitlist(event_id, 3)
    first = await service.cancel(event_id, 1)

    promotion = await service.expire_offer(
        first.promotion.waitlist_entry_id,
        now=first.promotion.expires_at + timedelta(seconds=1),
    )
    assert promotion is not None
    assert promotion.user_id == 3


async def test_manual_close_blocks_registration(
    service: RegistrationService, session_factory, event_id: int
) -> None:
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.status = EventStatus.REGISTRATION_CLOSED

    result = await service.register(event_id, 1)
    assert result.result == RegistrationResult.CLOSED
