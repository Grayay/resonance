from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from resonance_bot.db.models import Registration, RegistrationStatus
from resonance_bot.services.notifications import NotificationService


async def test_invitation_recipients_exclude_recent_registrations(
    session_factory, event_id: int
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                Registration(
                    event_id=event_id,
                    user_id=1,
                    status=RegistrationStatus.REGISTERED,
                    registered_at=now - timedelta(days=30),
                ),
                Registration(
                    event_id=event_id,
                    user_id=2,
                    status=RegistrationStatus.CANCELLED,
                    registered_at=now - timedelta(days=60),
                    cancelled_at=now - timedelta(days=59),
                ),
                Registration(
                    event_id=event_id,
                    user_id=3,
                    status=RegistrationStatus.REGISTERED,
                    registered_at=now - timedelta(days=91),
                ),
            ]
        )

    service = NotificationService(Mock(), session_factory, Mock())

    assert set(await service.invitation_recipient_ids()) == {3, 4, 5}
