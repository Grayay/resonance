from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import ValidationError
from sqlalchemy import select

from resonance_bot.config import Settings
from resonance_bot.db.models import Admin, Event, EventStatus
from resonance_bot.handlers import admin
from resonance_bot.services.users import is_admin
from resonance_bot.states import AdminManagement

BASE_SETTINGS = dict(
    bot_token="123456:TEST",
    database_url="sqlite+aiosqlite://",
    community_contact="@organizer",
    bot_username="test_bot",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in [
        "SUPERADMIN_IDS",
        "SUPERADMIN_ID",
        "MAIN_CHANNEL_ID",
        "CHANNEL_ID",
        "TEST_CHANNEL_ID",
        "DEFAULT_REMINDER_HOURS",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        **BASE_SETTINGS,
        superadmin_ids={1, 2},
        main_channel_id="-1001",
        test_channel_id="-1002",
    )


def message(actor, text=None):
    return SimpleNamespace(from_user=SimpleNamespace(id=actor), text=text, answer=AsyncMock())


def state_for(actor):
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=actor, user_id=actor))


async def test_admin_event_lists_filter_before_pagination(event_id, session_factory, settings):
    async with session_factory() as session, session.begin():
        future = await session.get(Event, event_id)
        future.status = EventStatus.DRAFT
        for index in range(31):
            session.add(
                Event(
                    title=f"Past {index}",
                    description=future.description,
                    starts_at=datetime.now(UTC) - timedelta(days=index + 1),
                    venue_name=future.venue_name,
                    address=future.address,
                    map_url=future.map_url,
                    capacity=1,
                    status=EventStatus.FINISHED,
                    created_by=1,
                    reminder_offsets_hours=[],
                )
            )

    def buttons(msg):
        return [row[0] for row in msg.answer.call_args.kwargs["reply_markup"].inline_keyboard]

    upcoming = message(1, "Мероприятия")
    await admin.admin_events(upcoming, session_factory, settings)
    assert [button.callback_data for button in buttons(upcoming)] == [
        f"admin:event:{event_id}:view"
    ]

    past = message(1, "Прошедшие мероприятия")
    await admin.admin_events(past, session_factory, settings)
    assert [button.text.split(" · ")[0] for button in buttons(past)[:-1]] == [
        f"Past {index}" for index in range(30)
    ]
    next_page = SimpleNamespace(
        from_user=past.from_user,
        message=past,
        data=buttons(past)[-1].callback_data,
        answer=AsyncMock(),
    )
    await admin.admin_events_page(next_page, session_factory, settings)
    assert buttons(past)[0].text.startswith("Past 30 ·")
    assert buttons(past)[1].callback_data == "admin:events:past:0"


def test_dotenv_new_values_take_precedence_and_parse_csv(tmp_path):
    dotenv = tmp_path / "bot.env"
    dotenv.write_text(
        "SUPERADMIN_IDS= 1312837656, 2081888103,1312837656\nSUPERADMIN_ID=999\n"
        "MAIN_CHANNEL_ID=-1001\nCHANNEL_ID=-1009\nTEST_CHANNEL_ID= -1002 \n"
        "DEFAULT_REMINDER_HOURS=24,3\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=dotenv, **BASE_SETTINGS)
    assert settings.superadmin_ids == {1312837656, 2081888103}
    assert settings.main_channel_id == "-1001"
    assert settings.test_channel_id == "-1002"
    assert settings.default_reminder_hours == [24, 3]


def test_legacy_values_still_work(monkeypatch):
    monkeypatch.setenv("SUPERADMIN_ID", "123456789")
    monkeypatch.setenv("CHANNEL_ID", "@legacy")
    settings = Settings(_env_file=None, **BASE_SETTINGS)
    assert settings.superadmin_ids == {123456789}
    assert settings.channel_id_for("main") == "@legacy"
    with pytest.raises(ValueError, match="TEST_CHANNEL_ID"):
        settings.channel_id_for("test")


@pytest.mark.parametrize("value", ["", "1,,2", "0", "-1", "1.5", "1,word", "[]"])
def test_invalid_superadmin_ids_are_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **BASE_SETTINGS, superadmin_ids=value, main_channel_id="@main")


def test_existing_json_reminder_config_is_preserved(monkeypatch):
    monkeypatch.setenv("DEFAULT_REMINDER_HOURS", "[24, 3]")
    settings = Settings(
        _env_file=None,
        **BASE_SETTINGS,
        superadmin_ids=" 1, 2, 1 ",
        main_channel_id="@main",
    )
    assert settings.superadmin_ids == {1, 2}
    assert settings.default_reminder_hours == [24, 3]


@pytest.mark.parametrize("actor", [1, 2])
async def test_both_superadmins_have_panel_and_admin_management_access(
    actor,
    session_factory,
    settings,
):
    async with session_factory() as session:
        assert await is_admin(session, actor, settings.superadmin_ids)
        assert await session.scalar(select(Admin.id).where(Admin.telegram_user_id == actor)) is None
    msg = message(actor)
    await admin.admin_command(msg, session_factory, settings)
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    assert any(button.text == "Администраторы" for row in keyboard.keyboard for button in row)
    await admin.admin_management(msg, session_factory, settings)
    state = state_for(actor)
    cb = SimpleNamespace(
        from_user=msg.from_user,
        data="admins:add",
        answer=AsyncMock(),
        message=msg,
    )
    await admin.admin_management_choice(cb, state, settings)
    assert await state.get_state() == AdminManagement.add.state
    await admin.add_admin(message(actor, "3"), state, session_factory, settings)
    async with session_factory() as session:
        assert await session.scalar(select(Admin.id).where(Admin.telegram_user_id == 3))
    cb.data = "admins:remove"
    await admin.admin_management_choice(cb, state, settings)
    await admin.remove_admin(message(actor, "3"), state, session_factory, settings)
    async with session_factory() as session:
        assert await session.scalar(select(Admin.id).where(Admin.telegram_user_id == 3)) is None


async def test_regular_admin_does_not_have_superadmin_access(session_factory, settings):
    async with session_factory() as session, session.begin():
        session.add(Admin(telegram_user_id=3, added_by=1))
    msg = message(3)
    await admin.admin_command(msg, session_factory, settings)
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    assert all(button.text != "Администраторы" for row in keyboard.keyboard for button in row)
    await admin.admin_management(msg, session_factory, settings)
    assert "только суперадминистратор" in msg.answer.call_args.args[0]
    state = state_for(3)
    cb = SimpleNamespace(
        from_user=msg.from_user, data="admins:add", answer=AsyncMock(), message=msg
    )
    await admin.admin_management_choice(cb, state, settings)
    assert cb.answer.call_args.kwargs["show_alert"]
    await state.set_state(AdminManagement.add)
    await admin.add_admin(message(3, "4"), state, session_factory, settings)
    await state.set_state(AdminManagement.remove)
    await admin.remove_admin(message(3, "3"), state, session_factory, settings)
    async with session_factory() as session:
        assert list(await session.scalars(select(Admin.telegram_user_id))) == [3]


@pytest.mark.parametrize("actor,target", [(1, 1), (1, 2), (2, 1), (2, 2)])
async def test_configured_superadmins_cannot_be_removed(actor, target, session_factory, settings):
    async with session_factory() as session, session.begin():
        session.add(Admin(telegram_user_id=target, added_by=actor))
    msg, state = message(actor, str(target)), state_for(actor)
    await state.set_state(AdminManagement.remove)
    await admin.remove_admin(msg, state, session_factory, settings)
    assert ".env" in msg.answer.call_args.args[0]
    async with session_factory() as session:
        assert await session.scalar(select(Admin.id).where(Admin.telegram_user_id == target))


async def test_configured_superadmin_is_not_added_as_database_admin(session_factory, settings):
    msg, state = message(1, "2"), state_for(1)
    await state.set_state(AdminManagement.add)
    await admin.add_admin(msg, state, session_factory, settings)
    async with session_factory() as session:
        assert list(await session.scalars(select(Admin))) == []
        assert await is_admin(session, 1312837656, {1312837656, 2081888103})
