from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User
from sqlalchemy import func, select

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    ChannelPublication,
    Event,
    EventStatus,
    PaymentReceipt,
    Registration,
    Reminder,
    WaitlistEntry,
)
from resonance_bot.handlers.admin import _creation_preview_text, router
from resonance_bot.services.notifications import NotificationService
from resonance_bot.states import EventCreation


def visible_text(value: str) -> str:
    class TextParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = TextParser()
    parser.feed(value)
    return "".join(parser.parts)


@pytest_asyncio.fixture
async def creation(session_factory, monkeypatch):
    settings = Settings(
        _env_file=None,
        bot_token="123456:TEST",
        superadmin_id=1,
        database_url="sqlite+aiosqlite://",
        main_channel_id="@main_channel",
        test_channel_id="@test_channel",
        community_contact="@organizer",
        bot_username="test_bot",
    )
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=bot.id, chat_id=1, user_id=1))
    requests, outgoing = [], []
    admin = User(id=1, is_bot=False, first_name="Admin")
    bot_user = User(id=bot.id, is_bot=True, first_name="Bot")

    async def telegram(method, **kwargs):
        requests.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        assert isinstance(method, (SendMessage, SendPhoto))
        text = method.text if isinstance(method, SendMessage) else method.caption or ""
        if isinstance(method, SendPhoto) and len(visible_text(text)) > 1024:
            raise TelegramBadRequest(
                method=method, message="Bad Request: message caption is too long"
            )
        if isinstance(method, SendMessage) and len(visible_text(text)) > 4096:
            raise TelegramBadRequest(method=method, message="Bad Request: message is too long")
        sent = Message(
            message_id=len(requests),
            date=datetime.now(UTC),
            from_user=bot_user,
            chat=Chat(id=1 if method.chat_id == 1 else -100123, type="private"),
            text=text if isinstance(method, SendMessage) else None,
            caption=text if isinstance(method, SendPhoto) else None,
            photo=[PhotoSize(file_id="cover", file_unique_id="cover-unique", width=1, height=1)]
            if isinstance(method, SendPhoto)
            else None,
            reply_markup=method.reply_markup,
        ).as_(bot)
        outgoing.append(sent)
        return sent

    # TelegramMethod.emit calls the Bot class's special method.
    monkeypatch.setattr(Bot, "__call__", lambda self, method, **kwargs: telegram(method, **kwargs))
    notifications = NotificationService(bot, session_factory, settings)

    async def message(text=None, *, photo=False):
        event = Message(
            message_id=1000 + len(requests),
            date=datetime.now(UTC),
            from_user=admin,
            chat=Chat(id=1, type="private"),
            text=text,
            photo=[PhotoSize(file_id="cover", file_unique_id="cover-unique", width=1, height=1)]
            if photo
            else None,
        ).as_(bot)
        result = await router.message.trigger(
            event,
            bot=bot,
            raw_state=await state.get_state(),
            state=state,
            session_factory=session_factory,
            settings=settings,
            notifications=notifications,
        )
        assert result is not UNHANDLED, f"Unmatched state {await state.get_state()}"

    async def callback(data):
        event = CallbackQuery(
            id=str(len(requests)),
            from_user=admin,
            chat_instance="test",
            data=data,
            message=outgoing[-1],
        ).as_(bot)
        result = await router.callback_query.trigger(
            event,
            bot=bot,
            raw_state=await state.get_state(),
            state=state,
            session_factory=session_factory,
            settings=settings,
            notifications=notifications,
        )
        assert result is not UNHANDLED, f"Unmatched callback {data}"

    yield SimpleNamespace(
        message=message,
        callback=callback,
        state=state,
        requests=requests,
        settings=settings,
        outgoing=outgoing,
    )
    await bot.session.close()


async def fill_event(
    creation,
    *,
    paid,
    cover=False,
    description="Description <safe> & text",
    target="main",
):
    await creation.message("Создать мероприятие")
    assert await creation.state.get_state() == EventCreation.channel.state
    await creation.callback(f"create:channel:{target}")
    await creation.message("Test event")
    await creation.message(description)
    assert await creation.state.get_state() == EventCreation.event_type.state
    await creation.callback("create:type:paid" if paid else "create:type:free")
    if paid:
        assert await creation.state.get_state() == EventCreation.price.state
        await creation.message("500")
        assert type((await creation.state.get_data())["price"]) is int
        assert await creation.state.get_state() == EventCreation.payment_details.state
        assert creation.outgoing[-1].text == (
            "Введите реквизиты для оплаты (например, номер телефона и банк):"
        )
        await creation.message("Банк & получатель")
    assert await creation.state.get_state() == EventCreation.event_date.state
    await creation.message(
        (datetime.now(creation.settings.tz) + timedelta(days=7)).strftime("%d.%m.%Y")
    )
    await creation.message("18:00")
    await creation.message("Venue")
    await creation.message("Address")
    await creation.message("https://example.com/map")
    if cover:
        await creation.message(photo=True)
    else:
        await creation.message("Пропустить")
    await creation.message("10")
    await creation.message("По умолчанию")
    await creation.message("Пропустить")
    assert await creation.state.get_state() == EventCreation.preview.state


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("publish", [False, True])
@pytest.mark.parametrize("cover", [False, True])
@pytest.mark.parametrize("target", ["main", "test"])
async def test_complete_creation_flow(creation, session_factory, paid, publish, cover, target):
    await fill_event(creation, paid=paid, cover=cover, target=target)
    preview = "\n".join(message.text or message.caption or "" for message in creation.outgoing)
    assert ("🎟 билет = 500 ₽" in preview) == paid
    await creation.callback("create:publish" if publish else "create:save")
    async with session_factory() as session:
        event = await session.scalar(select(Event))
        assert event.is_paid == paid
        assert event.publication_target == target
        assert event.price == (500 if paid else None)
        assert event.payment_details == ("Банк & получатель" if paid else None)
        assert event.status == (EventStatus.REGISTRATION_OPEN if publish else EventStatus.DRAFT)
        assert bool(event.published_at) == publish
        assert int(await session.scalar(select(func.count(ChannelPublication.id)))) == int(publish)
        for model in [Registration, PaymentReceipt, Reminder, WaitlistEntry]:
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    channel_id = creation.settings.channel_id_for(target)
    announcements = [r for r in creation.requests if getattr(r, "chat_id", None) == channel_id]
    assert bool(announcements) == publish
    if publish:
        text = "\n".join(
            getattr(r, "text", None) or getattr(r, "caption", None) or "" for r in announcements
        )
        assert ("🎟 билет = 500 ₽" in text) == paid
    else:
        # A later admin callback publishes from persisted event data, without
        # relying on the creation FSM or a shared "current channel" setting.
        await creation.callback(f"admin:event:{event.id}:publish")
        async with session_factory() as session:
            publication = await session.scalar(select(ChannelPublication))
            assert publication.channel_id == channel_id
    assert await creation.state.get_state() is None


async def test_unconfigured_test_channel_is_rejected_in_creation(creation):
    creation.settings.test_channel_id = None
    await creation.message("Создать мероприятие")
    await creation.callback("create:channel:test")
    assert await creation.state.get_state() == EventCreation.channel.state
    assert "publication_target" not in await creation.state.get_data()
    assert creation.requests[-1].show_alert
    await creation.callback("create:channel:main")
    assert await creation.state.get_state() == EventCreation.title.state


async def test_missing_creation_target_never_defaults_to_main(creation, session_factory):
    await fill_event(creation, paid=True, target="test")
    data = await creation.state.get_data()
    del data["publication_target"]
    await creation.state.set_data(data)
    await creation.callback("create:publish")
    assert creation.requests[-1].show_alert
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_channel_can_change_in_preview_but_not_after_publication(creation, session_factory):
    await fill_event(creation, paid=True)
    await creation.callback("create:field:publication_target")
    await creation.callback("create:channel:test")
    assert await creation.state.get_state() == EventCreation.preview.state
    assert (await creation.state.get_data())["price"] == 500
    await creation.callback("create:field:payment_details")
    await creation.message("Другой банк")
    assert await creation.state.get_state() == EventCreation.preview.state
    await creation.callback("create:publish")
    async with session_factory() as session:
        event = await session.scalar(select(Event))
        assert event.payment_details == "Другой банк"
    await creation.callback(f"admin:event:{event.id}:edit")
    await creation.callback(f"admin:editfield:{event.id}:publication_target")
    assert creation.requests[-1].show_alert
    async with session_factory() as session:
        assert (await session.get(Event, event.id)).publication_target == "test"
        assert (await session.scalar(select(ChannelPublication))).channel_id == "@test_channel"


@pytest.mark.parametrize("paid", [False, True])
async def test_long_preview_keeps_all_fields_and_can_publish(creation, session_factory, paid):
    await fill_event(creation, paid=paid, cover=True, description="Описание " * 300)
    assert creation.outgoing[-2].photo
    assert ("Описание " * 300).strip() in creation.outgoing[-1].text
    assert creation.outgoing[-1].reply_markup
    await creation.callback("create:publish")
    assert await creation.state.get_state() is None
    async with session_factory() as session:
        assert (await session.scalar(select(Event))).status == EventStatus.REGISTRATION_OPEN


async def test_invalid_price_keeps_price_state(creation):
    await creation.message("Создать мероприятие")
    await creation.callback("create:channel:main")
    await creation.message("Test event")
    await creation.message("Description")
    await creation.callback("create:type:paid")
    for invalid in ["0", "-1", "500.50", "invalid"]:
        await creation.message(invalid)
        assert await creation.state.get_state() == EventCreation.price.state
        assert (await creation.state.get_data())["price"] is None
    await creation.message("500")
    assert (await creation.state.get_data())["price"] == 500
    assert await creation.state.get_state() == EventCreation.payment_details.state
    await creation.message(photo=True)
    assert await creation.state.get_state() == EventCreation.payment_details.state
    await creation.message("Банк и получатель")
    assert await creation.state.get_state() == EventCreation.event_date.state


async def test_paid_preview_crosses_photo_caption_limit(creation, session_factory):
    # Same fields: the free preview fits a caption, the ticket line pushes the
    # paid preview beyond Telegram's limit before a DB insert is attempted.
    data = dict(
        title="Test event",
        description="",
        is_paid=False,
        price=None,
        event_date=(datetime.now(creation.settings.tz) + timedelta(days=7)).date().isoformat(),
        start_time="18:00",
        venue_name="Venue",
        address="Address",
        capacity=10,
        reminder_offsets_hours=[24, 3],
        feedback_delay_hours=None,
    )
    base_length = len(visible_text(_creation_preview_text(data, creation.settings)))
    description = "x" * (1010 - base_length)
    data["description"] = description
    assert len(visible_text(_creation_preview_text(data, creation.settings))) == 1010
    data.update(is_paid=True, price=500)
    assert len(visible_text(_creation_preview_text(data, creation.settings))) > 1024
    await fill_event(creation, paid=True, cover=True, description=description)
    assert len(creation.outgoing[-1].text or "") > 1024
    assert creation.outgoing[-2].photo
    await creation.callback("create:save")
    async with session_factory() as session:
        assert (await session.scalar(select(Event))).price == 500


async def test_creation_preview_keeps_caption_with_escaped_text(creation):
    description = "🎉 Встреча & общение <вживую>. " * 27 + "До встречи!"
    await fill_event(creation, paid=True, cover=True, description=description)
    preview = creation.outgoing[-1]
    assert preview.photo
    assert preview.caption and len(preview.caption) > 1024
    assert description in visible_text(preview.caption)
