from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, PhotoSize
from aiogram.types import Message as TelegramMessage
from aiogram.types import User as TelegramUser
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    Event,
    EventStatus,
    Feedback,
    JobStatus,
    PaymentReceipt,
    PaymentStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    ReminderKind,
)
from resonance_bot.handlers import admin, user
from resonance_bot.keyboards import day_reminder_keyboard, event_keyboard, reminder_keyboard
from resonance_bot.scheduler import PersistentScheduler
from resonance_bot.services.events import reschedule_registration_jobs
from resonance_bot.services.payments import (
    PAYMENT_INSTRUCTION,
    RECEIPT_CONFIRMATION,
    REFUND_MESSAGE,
    attach_receipt,
    event_receipts,
    payment_counts,
    payment_participants,
    positive_price,
    receipt_used,
)
from resonance_bot.services.registration import RegistrationResult, as_utc
from resonance_bot.states import EventCreation, EventEdit, FeedbackComment, ReceiptSelection
from resonance_bot.utils.formatting import event_announcement, event_card


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        bot_token="123456:TEST",
        superadmin_id=1,
        database_url="sqlite+aiosqlite://",
        channel_id="@test",
        community_contact="@admin",
        bot_username="test_bot",
    )


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


@pytest.fixture
def make_event(session_factory):
    async def create(*, paid=True, title="Клуб", price=500, capacity=5):
        async with session_factory() as session, session.begin():
            event = Event(
                title=title,
                description="Описание",
                starts_at=datetime.now(UTC) + timedelta(days=7),
                venue_name="Кафе",
                address="Адрес",
                map_url="https://example.com",
                capacity=capacity,
                status=EventStatus.REGISTRATION_OPEN,
                created_by=1,
                is_paid=paid,
                price=price if paid else None,
                reminder_offsets_hours=[],
            )
            session.add(event)
            await session.flush()
            return event.id

    return create


def message(*, user_id=1, file_id="receipt", unique_id="unique", text=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type="private"),
        message_id=10,
        text=text,
        photo=[SimpleNamespace(file_id=file_id, file_unique_id=unique_id)] if file_id else [],
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        edit_reply_markup=AsyncMock(),
        edit_media=AsyncMock(),
    )


def callback(data, *, user_id=1, photo=False):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        message=message(file_id="viewer" if photo else None),
    )


@pytest.mark.parametrize("paid", [False, True])
async def test_registration_confirmation(paid, make_event, service, session_factory, settings):
    event_id = await make_event(paid=paid)
    outcome = await service.register(event_id, 1)
    assert outcome.result == RegistrationResult.REGISTERED
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        if paid:
            event.price = 700

    settings = settings.model_copy(update={"payment_details": "Банк & получатель"})
    msg = message()
    await user._send_registration_success(msg, outcome.registration_id, session_factory, settings)
    confirmation = msg.answer.call_args.args[0]
    if paid:
        assert confirmation == (
            "Спасибо за запись! Вы записаны на «Клуб».\n\n"
            "К оплате: 500 ₽\n"
            "Реквизиты: Банк &amp; получатель\n\n"
            "После оплаты пришлите, пожалуйста, скриншот чека в этот чат."
        )
    else:
        date = user.human_datetime(event.starts_at, settings.tz)
        day, time = date.split(", ")
        assert confirmation == (
            f"<b>Записал тебя!</b>\n\nКлуб\n{day}, начало в {time}\nКафе\nАдрес\n\n"
            "Если планы поменяются — пожалуйста, нажми «Не смогу», и место перейдёт другому."
        )


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(hours=25),
        timedelta(hours=24),
        timedelta(hours=24, seconds=-1),
        timedelta(seconds=-1),
    ],
)
async def test_registered_event_card_cancellation(
    offset, make_event, service, session_factory, settings, monkeypatch
):
    now = datetime.now(UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(user, "datetime", FixedDatetime)
    event_id = await make_event()
    assert (await service.register(event_id, 1)).result == RegistrationResult.REGISTERED
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.starts_at = now + offset

    msg = message()
    await user.send_event_card(msg, event_id, 1, session_factory, settings)
    if offset < timedelta(0):
        msg.answer.assert_awaited_once_with("Это мероприятие уже прошло.")
        return

    text = msg.answer.call_args.args[0]
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    original = event_card(event, settings, active=1)
    if offset >= timedelta(hours=24):
        assert f"cancel:ask:{event_id}" in buttons
        assert text == original
    else:
        assert f"cancel:ask:{event_id}" not in buttons
        assert text == (
            original + "\n\nЕсли планы изменились и вы не сможете прийти, пожалуйста, "
            'напишите организатору: <a href="https://t.me/admin">@admin</a>'
        )


def scheduler(session_factory, service, settings):
    return PersistentScheduler(
        SimpleNamespace(send_message=AsyncMock()),
        session_factory,
        settings,
        service,
        Mock(),
    )


async def registrations(session_factory):
    async with session_factory() as session:
        return list(await session.scalars(select(Registration).order_by(Registration.id)))


async def receipts(session_factory):
    async with session_factory() as session:
        return list(await session.scalars(select(PaymentReceipt).order_by(PaymentReceipt.id)))


async def test_free_registration_is_unchanged(service, event_id, session_factory):
    await service.register(event_id, 1)
    reg = (await registrations(session_factory))[0]
    assert reg.payment_status == PaymentStatus.NOT_REQUIRED
    assert reg.payment_amount is None
    assert await receipts(session_factory) == []


async def test_pending_payment_reserves_capacity(service, make_event, session_factory):
    event_id = await make_event(capacity=1)
    await service.register(event_id, 1)
    assert (await service.register(event_id, 2)).result == RegistrationResult.FULL
    assert (await registrations(session_factory))[0].payment_status == PaymentStatus.PENDING


async def test_single_pending_photo_attaches_automatically(
    service,
    make_event,
    session_factory,
    state,
):
    event_id = await make_event()
    await service.register(event_id, 1)
    msg = message()
    await user.receipt_photo(msg, state, session_factory)
    reg = (await registrations(session_factory))[0]
    assert reg.payment_status == PaymentStatus.RECEIVED
    assert (await receipts(session_factory))[0].file_id == "receipt"
    assert (await receipts(session_factory))[0].received_at is not None
    msg.answer.assert_awaited_once_with(RECEIPT_CONFIRMATION)
    assert await state.get_state() is None


async def test_multiple_pending_photo_requires_selection_and_isolates_payment(
    service,
    make_event,
    session_factory,
    state,
):
    first, second = await make_event(title="Первый"), await make_event(title="Второй")
    await service.register(first, 1)
    selected = await service.register(second, 1)
    await service.register(first, 2)
    msg = message()
    await user.receipt_photo(msg, state, session_factory)
    assert await state.get_state() == ReceiptSelection.event.state
    assert all(
        reg.payment_status == PaymentStatus.PENDING for reg in await registrations(session_factory)
    )
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    assert [row[0].text for row in keyboard.inline_keyboard] == ["Первый", "Второй"]
    cb = callback(f"receipt:10:{selected.registration_id}")
    await user.receipt_selection(cb, state, session_factory)
    regs = await registrations(session_factory)
    assert [reg.payment_status for reg in regs] == [
        PaymentStatus.PENDING,
        PaymentStatus.RECEIVED,
        PaymentStatus.PENDING,
    ]
    assert await state.get_data() == {}
    cb.message.answer.assert_awaited_once_with(RECEIPT_CONFIRMATION)


async def test_arbitrary_photo_is_not_payment(session_factory, state):
    msg = message()
    await user.receipt_photo(msg, state, session_factory)
    assert "нет платной записи" in msg.answer.call_args.args[0]
    assert await registrations(session_factory) == []


async def test_duplicate_receipt_cannot_pay_second_event(
    service,
    make_event,
    session_factory,
    state,
):
    first = await service.register(await make_event(), 1)
    await service.register(await make_event(), 1)
    assert await attach_receipt(
        session_factory,
        first.registration_id,
        1,
        "receipt",
        file_unique_id="unique",
    )
    # Telegram can issue a different file_id for the same file_unique_id.
    msg = message(file_id="another-file-id")
    await user.receipt_photo(msg, state, session_factory)
    assert "уже получен" in msg.answer.call_args.args[0]
    assert [r.payment_status for r in await registrations(session_factory)] == [
        PaymentStatus.RECEIVED,
        PaymentStatus.PENDING,
    ]


async def test_new_photo_does_not_overwrite_paid_receipt(
    service,
    make_event,
    session_factory,
    state,
):
    reg = await service.register(await make_event(), 1)
    assert await attach_receipt(session_factory, reg.registration_id, 1, "original")
    await user.receipt_photo(message(file_id="new"), state, session_factory)
    assert (await receipts(session_factory))[0].file_id == "original"


async def test_stale_selection_after_cancellation_is_rejected(
    service,
    make_event,
    session_factory,
    state,
):
    first = await make_event()
    reg = await service.register(first, 1)
    await service.register(await make_event(), 1)
    await user.receipt_photo(message(), state, session_factory)
    await service.cancel(first, 1)
    await user.receipt_selection(
        callback(f"receipt:10:{reg.registration_id}"), state, session_factory
    )
    assert await receipts(session_factory) == []
    assert await state.get_state() is None


async def test_receipt_cannot_attach_to_another_user(service, make_event, session_factory):
    reg = await service.register(await make_event(), 2)
    assert not await attach_receipt(session_factory, reg.registration_id, 1, "wrong")


async def test_receipt_selection_replay_does_not_change_payment(
    service,
    make_event,
    session_factory,
    state,
):
    reg = await service.register(await make_event(), 1)
    cb = callback(f"receipt:10:{reg.registration_id}")
    await user.receipt_selection(cb, state, session_factory)
    assert (await registrations(session_factory))[0].payment_status == PaymentStatus.PENDING
    assert cb.answer.call_args.kwargs["show_alert"]


async def test_daily_reminders_are_separate_and_only_for_pending_paid(
    service,
    make_event,
    session_factory,
    settings,
):
    now = datetime.now(UTC)
    await service.register(await make_event(title="Первый"), 1, now=now - timedelta(days=2))
    await service.register(
        await make_event(title="Второй", price=700), 1, now=now - timedelta(days=2)
    )
    await service.register(await make_event(paid=False), 2, now=now - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    for _ in range(2):
        await worker._schedule_payment_reminders()
        await worker._process_due_reminders()
    assert worker.bot.send_message.await_count == 2
    texts = [call.args[1] for call in worker.bot.send_message.call_args_list]
    assert "«Первый» — 500 ₽" in texts[0]
    assert "«Второй» — 700 ₽" in texts[1]
    assert all(text.endswith(PAYMENT_INSTRUCTION) for text in texts)


@pytest.mark.parametrize("change", ["receipt", "cancel", "start", "cancel_event"])
async def test_payment_reminders_stop_after_state_change(
    change,
    service,
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event()
    reg = await service.register(event_id, 1, now=datetime.now(UTC) - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    job_id = await worker._claim_job()
    assert job_id is not None
    if change == "receipt":
        await attach_receipt(session_factory, reg.registration_id, 1, "receipt")
    elif change == "cancel":
        await service.cancel(event_id, 1)
    else:
        async with session_factory() as session, session.begin():
            event = await session.get(Event, event_id)
            if change == "start":
                event.starts_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                event.status = EventStatus.CANCELLED
    await worker._deliver_job(job_id)
    await worker._schedule_payment_reminders()
    await worker._process_due_reminders()
    worker.bot.send_message.assert_not_awaited()


async def test_payment_reminders_resume_next_local_day(
    service,
    make_event,
    session_factory,
    settings,
    monkeypatch,
):
    now = datetime.now(UTC)
    await service.register(await make_event(), 1, now=now - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    await worker._process_due_reminders()
    tomorrow = now + timedelta(days=1)

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow if tz else tomorrow.replace(tzinfo=None)

    monkeypatch.setattr("resonance_bot.scheduler.datetime", Tomorrow)
    await worker._schedule_payment_reminders()
    await worker._process_due_reminders()
    assert worker.bot.send_message.await_count == 2


async def test_free_events_never_get_payment_jobs(service, event_id, session_factory, settings):
    await service.register(event_id, 1, now=datetime.now(UTC) - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    async with session_factory() as session:
        assert list(await session.scalars(select(Reminder))) == []


async def test_cancellation_requires_confirmation(service, event_id, session_factory):
    await service.register(event_id, 1)
    cb = callback(f"cancel:ask:{event_id}")
    await user.ask_cancel(cb)
    assert cb.message.answer.call_args.args[0] == "Точно отменить запись?"
    assert (await registrations(session_factory))[0].status == RegistrationStatus.REGISTERED
    cb.data = f"cancel:no:{event_id}"
    await user.keep_registration(cb)
    assert (await registrations(session_factory))[0].status == RegistrationStatus.REGISTERED


@pytest.mark.parametrize("received", [False, True])
async def test_paid_cancellation_frees_seat_and_refund_message(
    received,
    service,
    make_event,
    session_factory,
):
    event_id = await make_event(capacity=1)
    reg = await service.register(event_id, 1)
    if received:
        await attach_receipt(session_factory, reg.registration_id, 1, "receipt")
    cb = callback(f"cancel:yes:{event_id}")
    await user.cancel_registration(cb, service, Mock())
    assert (await registrations(session_factory))[0].status == RegistrationStatus.CANCELLED
    assert (await service.register(event_id, 2)).result == RegistrationResult.REGISTERED
    if received:
        cb.message.answer.assert_awaited_once_with(REFUND_MESSAGE)
        async with session_factory() as session:
            assert len(await event_receipts(session, event_id, 0)) == 1


async def test_paid_waitlist_promotion_starts_payment_flow(
    service,
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event(capacity=1)
    await service.register(event_id, 1)
    await service.join_waitlist(event_id, 2)
    promotion = (await service.cancel(event_id, 1)).promotion
    cb = callback(f"offer:accept:{promotion.waitlist_entry_id}", user_id=2)
    await user.accept_offer(cb, service, session_factory, settings, Mock())
    assert (await registrations(session_factory))[1].payment_status == PaymentStatus.PENDING
    confirmation = cb.message.answer.call_args.args[0]
    assert confirmation.endswith("После оплаты пришлите, пожалуйста, скриншот чека в этот чат.")
    assert (await service.register(event_id, 3)).result == RegistrationResult.FULL


async def test_paid_and_pending_admin_queries_are_scoped_and_active(
    service,
    make_event,
    session_factory,
):
    first, second = await make_event(), await make_event()
    paid = await service.register(first, 1)
    cancelled = await service.register(first, 2)
    await service.register(first, 3)
    await service.register(second, 4)
    await attach_receipt(session_factory, paid.registration_id, 1, "paid")
    await attach_receipt(session_factory, cancelled.registration_id, 2, "cancelled")
    await service.cancel(first, 2)
    async with session_factory() as session:
        assert await payment_counts(session, first) == (1, 1)
        assert [r.user_id for r, _ in await payment_participants(session, first)] == [1, 3]
        assert [
            r.user_id
            for r, _ in await payment_participants(
                session,
                first,
                PaymentStatus.RECEIVED,
            )
        ] == [1]
        assert [
            r.user_id
            for r, _ in await payment_participants(
                session,
                first,
                PaymentStatus.PENDING,
            )
        ] == [3]


async def test_admin_receipts_show_correct_event_with_pagination(
    service,
    make_event,
    session_factory,
    settings,
):
    first, second = await make_event(title="Первый"), await make_event(title="Второй")
    for event_id, user_id, file_id in [(first, 1, "one"), (first, 2, "two"), (second, 1, "other")]:
        reg = await service.register(event_id, user_id)
        await attach_receipt(session_factory, reg.registration_id, user_id, file_id)
    cb = callback(f"payments:{first}:receipts:0")
    await admin.admin_payments(cb, session_factory, settings)
    cb.message.answer_photo.assert_awaited_once()
    args = cb.message.answer_photo.call_args
    assert args.args[0] == "one"
    assert "Первый" in args.kwargs["caption"]
    assert "Telegram ID: 1" in args.kwargs["caption"]
    assert "500 ₽" in args.kwargs["caption"]
    assert any(
        b.callback_data == f"payments:{first}:receipts:1"
        for row in args.kwargs["reply_markup"].inline_keyboard
        for b in row
    )
    cb = callback(f"payments:{first}:receipts:1", photo=True)
    await admin.admin_payments(cb, session_factory, settings)
    assert cb.message.edit_media.call_args.args[0].media == "two"
    cb.message.answer_photo.assert_not_awaited()


async def test_admin_empty_receipts_and_unauthorized_access(
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event()
    cb = callback(f"payments:{event_id}:receipts:0")
    await admin.admin_payments(cb, session_factory, settings)
    cb.message.answer.assert_awaited_once_with("Чеков пока нет.")
    cb = callback(f"payments:{event_id}:receipts:0", user_id=5)
    await admin.admin_payments(cb, session_factory, settings)
    cb.message.answer_photo.assert_not_awaited()
    assert cb.answer.call_args.kwargs["show_alert"]


@pytest.mark.parametrize("raw", ["0", "-1", "5.5", "abc", "", "2147483648"])
def test_invalid_prices_are_rejected(raw):
    with pytest.raises(ValueError):
        positive_price(raw)


async def test_price_consistency_constraint(make_event, session_factory):
    event_id = await make_event()
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            (await session.get(Event, event_id)).price = 0


async def test_channel_announcement_price_position_and_free_unchanged(
    make_event,
    session_factory,
    settings,
):
    paid, free = await make_event(), await make_event(paid=False)
    async with session_factory() as session:
        paid_text = event_announcement(await session.get(Event, paid), settings)
        free_text = event_announcement(await session.get(Event, free), settings)
    assert paid_text.index("Описание") < paid_text.index("🎟 билет = 500 ₽")
    assert paid_text.index("🎟 билет = 500 ₽") < paid_text.index("<blockquote>")
    assert "билет" not in free_text and "₽" not in free_text


async def test_normal_reminders_survive_payment_and_edit(
    service,
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event()
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.reminder_offsets_hours = [24, 3]
        event.feedback_at = as_utc(event.starts_at) + timedelta(hours=4)
    reg = await service.register(event_id, 1, now=datetime.now(UTC) - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id)
        event.starts_at = as_utc(event.starts_at)
        event.feedback_at = as_utc(event.feedback_at)
        await reschedule_registration_jobs(session, event)
        assert (
            await session.scalar(
                select(Reminder).where(
                    Reminder.kind == ReminderKind.PAYMENT,
                )
            )
        ).status == JobStatus.PENDING
    await attach_receipt(session_factory, reg.registration_id, 1, "receipt")
    async with session_factory() as session:
        jobs = list(await session.scalars(select(Reminder)))
    assert all(j.status == JobStatus.PENDING for j in jobs if j.kind != ReminderKind.PAYMENT)
    assert {j.kind for j in jobs} == {
        ReminderKind.PAYMENT,
        ReminderKind.EVENT,
        ReminderKind.FEEDBACK,
    }


async def test_price_edit_and_free_type_restriction(
    make_event,
    session_factory,
    settings,
    state,
):
    event_id = await make_event()
    await state.set_state(EventEdit.value)
    await state.update_data(event_id=event_id, field="price")
    await admin.admin_save_edit(message(text="700", file_id=None), state, session_factory, settings)
    async with session_factory() as session:
        assert (await session.get(Event, event_id)).price == 700
    free = await make_event(paid=False)
    await state.set_state(EventEdit.choosing)
    cb = callback(f"admin:editfield:{free}:price")
    await admin.admin_choose_edit_field(cb, state, session_factory, settings)
    assert cb.answer.call_args.kwargs["show_alert"]
    assert await state.get_state() == EventEdit.choosing.state


async def test_creation_requires_explicit_type_and_price(state, settings):
    await state.set_state(EventCreation.description)
    await admin.create_description(message(text="Цена 500 рублей", file_id=None), state, settings)
    assert await state.get_state() == EventCreation.event_type.state
    assert "is_paid" not in await state.get_data()
    await admin.create_type(callback("create:type:paid"), state, settings)
    assert await state.get_state() == EventCreation.price.state
    await admin.create_price(message(text="0", file_id=None), state, settings)
    assert await state.get_state() == EventCreation.price.state
    await admin.create_price(message(text="500", file_id=None), state, settings)
    assert (await state.get_data())["price"] == 500
    assert await state.get_state() == EventCreation.event_date.state


async def test_all_cancellation_entry_points_use_confirmation(
    make_event, session_factory, settings
):
    event_id = await make_event()
    async with session_factory() as session:
        event = await session.get(Event, event_id)
    for keyboard in [
        event_keyboard(event, settings, registered=True),
        reminder_keyboard(event),
        day_reminder_keyboard(event),
    ]:
        buttons = [b for row in keyboard.inline_keyboard for b in row if b.text == "Не смогу"]
        assert buttons[0].callback_data == f"cancel:ask:{event_id}"


async def test_repeat_register_keeps_received_payment(
    service,
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event()
    reg = await service.register(event_id, 1)
    await attach_receipt(session_factory, reg.registration_id, 1, "original")
    cb = callback(f"event:register:{event_id}")
    await user.register(cb, service, session_factory, settings)
    assert "К оплате:" not in cb.message.answer.call_args.args[0]
    stored = (await registrations(session_factory))[0]
    assert stored.payment_status == PaymentStatus.RECEIVED
    assert (await receipts(session_factory))[0].file_id == "original"


async def test_no_receipt_or_reminder_after_event_start(
    service,
    make_event,
    session_factory,
    state,
):
    event_id = await make_event()
    await service.register(event_id, 1)
    async with session_factory() as session, session.begin():
        (await session.get(Event, event_id)).starts_at = datetime.now(UTC) - timedelta(seconds=1)
    msg = message()
    await user.receipt_photo(msg, state, session_factory)
    assert "нет платной записи" in msg.answer.call_args.args[0]
    assert (await registrations(session_factory))[0].payment_status == PaymentStatus.PENDING


async def test_stale_photo_button_cannot_attach_newer_photo(
    service,
    make_event,
    session_factory,
    state,
):
    reg = await service.register(await make_event(), 1)
    await service.register(await make_event(), 1)
    await user.receipt_photo(message(file_id="old"), state, session_factory)
    msg = message(file_id="new")
    msg.message_id = 11
    await user.receipt_photo(msg, state, session_factory)
    await user.receipt_selection(
        callback(f"receipt:10:{reg.registration_id}"), state, session_factory
    )
    assert (await state.get_data())["receipt_file_id"] == "new"
    await user.receipt_selection(
        callback(f"receipt:11:{reg.registration_id}"), state, session_factory
    )
    assert (await receipts(session_factory))[0].file_id == "new"


async def test_payment_delivery_failure_is_not_retried_same_day(
    service,
    make_event,
    session_factory,
    settings,
):
    await service.register(await make_event(), 1, now=datetime.now(UTC) - timedelta(days=2))
    worker = scheduler(session_factory, service, settings)
    worker.bot.send_message.side_effect = TelegramAPIError(method=Mock(), message="Network failure")
    for _ in range(2):
        await worker._schedule_payment_reminders()
        await worker._process_due_reminders()
    worker.bot.send_message.assert_awaited_once()
    async with session_factory() as session:
        assert (await session.scalar(select(Reminder))).status == JobStatus.FAILED


async def test_stale_payment_job_is_skipped_but_normal_job_retried(
    service,
    make_event,
    session_factory,
    settings,
):
    reg = await service.register(await make_event(), 1)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                Reminder(
                    registration_id=reg.registration_id,
                    kind=kind,
                    offset_hours=0,
                    status=JobStatus.PROCESSING,
                    scheduled_at=datetime.now(UTC),
                    attempts=1,
                    locked_at=datetime.now(UTC) - timedelta(minutes=11),
                )
                for kind in [ReminderKind.PAYMENT, ReminderKind.EVENT]
            ]
        )
    worker = scheduler(session_factory, service, settings)
    await worker._recover_stale_jobs()
    async with session_factory() as session:
        rows = list(await session.scalars(select(Reminder).order_by(Reminder.id)))
    assert [row.status for row in rows] == [JobStatus.SKIPPED, JobStatus.PENDING]


async def test_daily_jobs_use_local_day_and_start_after_registration_day(
    service,
    make_event,
    session_factory,
    settings,
):
    now = datetime.now(UTC)
    await service.register(await make_event(), 1, now=now)
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders(now=now)
    async with session_factory() as session:
        assert list(await session.scalars(select(Reminder))) == []
    tomorrow = now + timedelta(days=1)
    await worker._schedule_payment_reminders(now=tomorrow)
    async with session_factory() as session:
        job = await session.scalar(select(Reminder))
    local = as_utc(job.scheduled_at).astimezone(settings.tz)
    assert local.date() == tomorrow.astimezone(settings.tz).date()
    assert (local.hour, local.minute) == (0, 0)


async def test_admin_event_payment_statistics(service, make_event, session_factory, settings):
    event_id = await make_event()
    reg = await service.register(event_id, 1)
    await service.register(event_id, 2)
    await attach_receipt(session_factory, reg.registration_id, 1, "paid")
    cb = callback(f"admin:event:{event_id}:view")
    await admin.admin_view_event(cb, session_factory, settings)
    text = cb.message.answer.call_args.args[0]
    assert "Записано: <b>2/5</b>" in text
    assert "Оплатили: 1 · Ожидают оплату: 1" in text


@pytest.mark.parametrize("paid", [False, True])
async def test_save_created_event_has_consistent_payment_fields(
    paid,
    session_factory,
    settings,
    state,
):
    day = (datetime.now(settings.tz) + timedelta(days=1)).date()
    await state.set_state(EventCreation.preview)
    await state.update_data(
        title="Новое",
        publication_target="main",
        description="Описание",
        event_date=day.isoformat(),
        start_time="18:00",
        venue_name="Площадка",
        address="Адрес",
        map_url="https://example.com",
        capacity=10,
        reminder_offsets_hours=[24, 3],
        feedback_delay_hours=4,
        is_paid=paid,
        price=500 if paid else None,
    )
    await admin._save_created_event(
        callback("create:save"),
        state,
        session_factory,
        settings,
        Mock(),
        publish=False,
    )
    async with session_factory() as session:
        event = await session.scalar(select(Event))
    assert event.is_paid == paid
    assert event.price == (500 if paid else None)


async def test_receipt_history_survives_new_payment_cycle(
    service,
    make_event,
    session_factory,
    settings,
):
    event_id = await make_event()
    first = await service.register(event_id, 1)
    assert await attach_receipt(
        session_factory,
        first.registration_id,
        1,
        "old",
        file_unique_id="old-unique",
    )
    original = (await receipts(session_factory))[0]
    await service.cancel(event_id, 1)
    cb = callback(f"payments:{event_id}:receipts:0")
    await admin.admin_payments(cb, session_factory, settings)
    assert cb.message.answer_photo.call_args.args[0] == "old"
    assert "отменена" in cb.message.answer_photo.call_args.kwargs["caption"]

    async with session_factory() as session, session.begin():
        (await session.get(Event, event_id)).price = 700
    await service.register(event_id, 1)
    current = (await registrations(session_factory))[0]
    assert current.payment_status == PaymentStatus.PENDING
    assert current.payment_cycle == 2
    assert current.payment_amount == 700
    async with session_factory() as session:
        history = await event_receipts(session, event_id, 0)
        assert len(history) == 1
        assert await receipt_used(session, 1, "old", "old-unique")
    retained = history[0][0]
    assert retained.id == original.id
    assert retained.received_at == original.received_at
    assert retained.cancelled_at is not None
    assert retained.amount == 500
    assert not await attach_receipt(session_factory, current.id, 1, "old")
    assert not await attach_receipt(
        session_factory,
        current.id,
        1,
        "different-file-id",
        file_unique_id="old-unique",
    )
    assert (await registrations(session_factory))[0].payment_status == PaymentStatus.PENDING
    assert await attach_receipt(session_factory, current.id, 1, "new", file_unique_id="new-unique")
    stored = await receipts(session_factory)
    assert [(r.payment_cycle, r.file_id) for r in stored] == [(1, "old"), (2, "new")]
    cb = callback(f"payments:{event_id}:receipts:0")
    await admin.admin_payments(cb, session_factory, settings)
    assert cb.message.answer_photo.call_args.args[0] == "old"


async def test_price_edit_affects_future_registrations_only(
    service,
    make_event,
    session_factory,
    settings,
    state,
):
    event_id = await make_event()
    first = await service.register(event_id, 1, now=datetime.now(UTC) - timedelta(days=2))
    await state.set_state(EventEdit.value)
    await state.set_data(dict(event_id=event_id, field="price"))
    await admin.admin_save_edit(message(text="700", file_id=None), state, session_factory, settings)
    second = await service.register(event_id, 2, now=datetime.now(UTC) - timedelta(days=2))
    regs = await registrations(session_factory)
    assert [reg.payment_amount for reg in regs] == [500, 700]

    cb = callback(f"event:register:{event_id}")
    await user.register(cb, service, session_factory, settings)
    assert "500 ₽" in cb.message.answer.call_args.args[0]
    cb = callback(f"payments:{event_id}:pending:0")
    await admin.admin_payments(cb, session_factory, settings)
    text = cb.message.answer.call_args.args[0]
    assert "500 ₽" in text and "700 ₽" in text
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    await worker._process_due_reminders()
    assert "500 ₽" in worker.bot.send_message.call_args_list[0].args[1]
    assert "700 ₽" in worker.bot.send_message.call_args_list[1].args[1]

    await attach_receipt(session_factory, first.registration_id, 1, "paid-at-500")
    cb = callback(f"payments:{event_id}:paid:0")
    await admin.admin_payments(cb, session_factory, settings)
    assert "500 ₽" in cb.message.answer.call_args.args[0]
    cb = callback(f"payments:{event_id}:receipts:0")
    await admin.admin_payments(cb, session_factory, settings)
    assert "500 ₽" in cb.message.answer_photo.call_args.kwargs["caption"]
    assert "700 ₽" not in cb.message.answer_photo.call_args.kwargs["caption"]
    assert second.registration_id != first.registration_id


@pytest_asyncio.fixture
async def route_message(monkeypatch, state, session_factory):
    bot = Bot("123456:TEST")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(Bot, "__call__", send)

    async def route(*, text=None):
        msg = TelegramMessage(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=1, type="private"),
            from_user=TelegramUser(id=1, is_bot=False, first_name="User 1"),
            text=text,
            photo=None
            if text
            else [
                PhotoSize(
                    file_id="receipt",
                    file_unique_id="unique",
                    width=1,
                    height=1,
                )
            ],
        ).as_(bot)
        await user.router.message.trigger(
            msg,
            bot=bot,
            raw_state=await state.get_state(),
            state=state,
            session_factory=session_factory,
        )
        return send

    yield route
    await bot.session.close()


@pytest.mark.parametrize("pending_count", [1, 2])
async def test_receipt_routing_preserves_and_resumes_feedback(
    pending_count,
    service,
    event_id,
    make_event,
    session_factory,
    state,
    route_message,
):
    feedback_registration = await service.register(event_id, 1)
    async with session_factory() as session, session.begin():
        session.add(Feedback(registration_id=feedback_registration.registration_id, rating=5))
    resume = {"registration_id": feedback_registration.registration_id, "other": "preserved"}
    await state.set_state(FeedbackComment.text)
    await state.set_data(resume)
    for _ in range(pending_count):
        await service.register(await make_event(), 1)
    await route_message()
    if pending_count == 2:
        data = await state.get_data()
        assert await state.get_state() == ReceiptSelection.event.state
        selected = next(iter(data["receipt_cycles"]))
        await user.receipt_selection(callback(f"receipt:10:{selected}"), state, session_factory)
    assert await state.get_state() == FeedbackComment.text.state
    assert await state.get_data() == resume
    assert len(await receipts(session_factory)) == 1
    await route_message(text="Excellent")
    async with session_factory() as session:
        assert (await session.scalar(select(Feedback))).comment == "Excellent"
    assert await state.get_state() is None


async def test_no_pending_receipt_preserves_original_feedback_photo_behavior(
    service,
    event_id,
    session_factory,
    state,
    route_message,
):
    reg = await service.register(event_id, 1)
    async with session_factory() as session, session.begin():
        session.add(Feedback(registration_id=reg.registration_id, rating=5))
    await state.set_state(FeedbackComment.text)
    await state.set_data(dict(registration_id=reg.registration_id))
    send = await route_message()
    assert send.call_args.args[0].text == "Спасибо за отзыв!"
    assert await state.get_state() is None
    assert await receipts(session_factory) == []
    async with session_factory() as session:
        assert (await session.scalar(select(Feedback))).comment == ""


async def test_receipt_routing_preserves_other_user_state(
    service,
    make_event,
    state,
    route_message,
):
    await service.register(await make_event(), 1)
    await state.set_state("OtherUserFlow:input")
    await state.set_data({"draft": "keep"})
    await route_message()
    assert await state.get_state() == "OtherUserFlow:input"
    assert await state.get_data() == {"draft": "keep"}


async def test_old_selection_does_not_confirm_re_registration_cycle(
    service,
    make_event,
    session_factory,
    state,
):
    first = await make_event()
    reg = await service.register(first, 1)
    await service.register(await make_event(), 1)
    await user.receipt_photo(message(), state, session_factory)
    await service.cancel(first, 1)
    await service.register(first, 1)
    await user.receipt_selection(
        callback(f"receipt:10:{reg.registration_id}"), state, session_factory
    )
    assert await receipts(session_factory) == []
    assert all(
        r.payment_status == PaymentStatus.PENDING for r in await registrations(session_factory)
    )


async def test_existing_daily_payment_jobs_cost_only_one_query(
    service,
    make_event,
    session_factory,
    settings,
):
    for user_id in [1, 2, 3]:
        await service.register(
            await make_event(), user_id, now=datetime.now(UTC) - timedelta(days=2)
        )
    worker = scheduler(session_factory, service, settings)
    await worker._schedule_payment_reminders()
    statements = []
    engine = session_factory.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture)
    try:
        await worker._schedule_payment_reminders()
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "EXISTS" in statements[0]
    async with session_factory() as session:
        assert len(list(await session.scalars(select(Reminder)))) == 3


async def test_normal_reminder_dispatch_does_not_query_kind_separately(
    service,
    event_id,
    session_factory,
    settings,
):
    reg = await service.register(event_id, 1)
    async with session_factory() as session, session.begin():
        job = Reminder(
            registration_id=reg.registration_id,
            kind=ReminderKind.EVENT,
            offset_hours=3,
            scheduled_at=datetime.now(UTC),
            status=JobStatus.PROCESSING,
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    statements = []
    engine = session_factory.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture)
    try:
        await scheduler(session_factory, service, settings)._deliver_job(job_id)
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
