from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    ChannelPublication,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
)
from resonance_bot.services.notifications import NotificationService
from resonance_bot.utils.formatting import event_announcement


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


@pytest.fixture
def publication_settings():
    return Settings(
        _env_file=None,
        bot_token="123456:TEST",
        superadmin_ids={1, 2},
        database_url="sqlite+aiosqlite://",
        main_channel_id="-1001",
        test_channel_id="-1002",
        community_contact="@organizer",
        bot_username="test_bot",
    )


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("length", [980, 1024, 1025])
async def test_publication_caption_boundary(
    paid, length, event_id, session_factory, publication_settings
):
    sample = "🎉 Встреча & общение <вживую>. "
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.cover_file_id = "cover"
        event.is_paid, event.price = paid, 500 if paid else None
        event.description = ""
        base_text = event_announcement(event, publication_settings)
        base_length = len(unescape(re.sub(r"<[^>]+>", "", base_text)))
        event.description = (sample * length)[: length - base_length - 1] + "!"
        announcement = event_announcement(event, publication_settings)
        assert len(announcement) > 1024
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=1)
    bot.send_message.return_value = SimpleNamespace(message_id=2)
    service = NotificationService(bot, session_factory, publication_settings)
    publication = await service.publish_event(event_id)

    bot.send_photo.assert_awaited_once()
    assert publication.photo_message_id == 1
    if length <= 1024:
        assert bot.send_photo.call_args.kwargs["caption"] == announcement
        bot.send_message.assert_not_awaited()
        assert publication.text_message_id is None
    else:
        assert "caption" not in bot.send_photo.call_args.kwargs
        assert bot.send_message.call_args.args[1] == announcement
        assert publication.text_message_id == 2


@pytest.mark.parametrize("target", ["main", "test"])
@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("layout", ["no_cover", "caption", "separate_text", "long"])
async def test_publication_uses_event_target_and_stored_channel_for_removal(
    target,
    paid,
    layout,
    event_id,
    session_factory,
    publication_settings,
):
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.publication_target = target
        event.is_paid, event.price = paid, 500 if paid else None
        event.cover_file_id = None if layout == "no_cover" else "cover"
        if layout == "separate_text":
            event.description = "x" * 1000
        elif layout == "long":
            event.description = "x" * 3500
            event.address = "x" * 500
            event.venue_name = "x" * 255
    bot = AsyncMock()
    bot.send_message.return_value = bot.send_photo.return_value = SimpleNamespace(message_id=123)
    service = NotificationService(bot, session_factory, publication_settings)
    publication = await service.publish_event(event_id)
    expected = publication_settings.channel_id_for(target)
    assert publication.channel_id == expected
    calls = bot.send_message.call_args_list + bot.send_photo.call_args_list
    assert calls and all(call.args[0] == expected for call in calls)
    text = "\n".join(
        call.kwargs.get("caption", "")
        if len(call.args) < 2 or call.args[1] == "cover"
        else call.args[1]
        for call in calls
    )
    assert ("🎟 билет = 500 ₽" in text) == paid
    assert "Тестовый канал" not in text
    keyboards = [call.kwargs["reply_markup"] for call in calls if call.kwargs.get("reply_markup")]
    assert keyboards[0].inline_keyboard[0][0].url.endswith(f"?start=event_{event_id}")
    async with session_factory() as session:
        assert (await session.scalar(select(ChannelPublication))).channel_id == expected
    changed_settings = publication_settings.model_copy(
        update={
            "main_channel_id": "-1009",
            "test_channel_id": "-1008",
        }
    )
    await NotificationService(bot, session_factory, changed_settings).disable_channel_registration(
        event_id
    )
    assert bot.edit_message_reply_markup.call_args.kwargs["chat_id"] == expected
    assert bot.edit_message_reply_markup.call_args.kwargs["message_id"] == 123


async def test_publications_for_different_channels_do_not_interfere(
    event_id,
    session_factory,
    publication_settings,
):
    async with session_factory() as session, session.begin():
        first = await session.get(Event, event_id)
        first.title = "Main event"
        second = Event(
            title="Test event",
            description="Description",
            starts_at=first.starts_at,
            venue_name="Venue",
            address="Address",
            map_url="https://example.com",
            capacity=1,
            status=EventStatus.DRAFT,
            created_by=2,
            reminder_offsets_hours=[],
            publication_target="test",
        )
        session.add(second)
        await session.flush()
        second_id = second.id
    calls = []

    async def send(channel_id, text, **kwargs):
        calls.append((channel_id, text))
        message_id = len(calls)
        await asyncio.sleep(0)
        return SimpleNamespace(message_id=message_id)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    service = NotificationService(bot, session_factory, publication_settings)
    publications = await asyncio.gather(
        service.publish_event(event_id), service.publish_event(second_id)
    )
    assert [publication.channel_id for publication in publications] == ["-1001", "-1002"]
    assert all(("Main event" in text) == (channel == "-1001") for channel, text in calls)


async def test_missing_test_channel_never_falls_back_to_main(
    event_id,
    session_factory,
    publication_settings,
):
    async with session_factory() as session, session.begin():
        (await session.get(Event, event_id)).publication_target = "test"
    bot = AsyncMock()
    settings = publication_settings.model_copy(update={"test_channel_id": None})
    with pytest.raises(ValueError, match="TEST_CHANNEL_ID"):
        await NotificationService(bot, session_factory, settings).publish_event(event_id)
    assert bot.mock_calls == []
    async with session_factory() as session:
        assert (await session.get(Event, event_id)).published_at is None


async def test_existing_event_defaults_to_main_channel(
    event_id, session_factory, publication_settings
):
    async with session_factory() as session:
        assert (await session.get(Event, event_id)).publication_target == "main"
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    publication = await NotificationService(
        bot, session_factory, publication_settings
    ).publish_event(event_id)
    assert publication.channel_id == "-1001"
