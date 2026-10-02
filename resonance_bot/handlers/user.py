from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    Event,
    EventStatus,
    Feedback,
    Registration,
    RegistrationStatus,
    WaitlistEntry,
    WaitlistStatus,
)
from resonance_bot.keyboards import (
    cancel_confirm_keyboard,
    contact_keyboard,
    event_keyboard,
    user_menu,
)
from resonance_bot.services.notifications import NotificationService
from resonance_bot.services.registration import (
    OfferResult,
    RegistrationResult,
    RegistrationService,
    WaitlistResult,
)
from resonance_bot.states import FeedbackComment
from resonance_bot.utils.dates import human_datetime
from resonance_bot.utils.formatting import event_card, registration_success

router = Router(name="user")
PAGE_SIZE = 6


async def _event_and_state(
    session: AsyncSession, event_id: int, user_id: int
) -> tuple[Event | None, int, int, bool]:
    event = await session.get(Event, event_id)
    if event is None:
        return None, 0, 0, False
    active = int(
        await session.scalar(
            select(func.count(Registration.id)).where(
                Registration.event_id == event_id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
        or 0
    )
    registered = (
        await session.scalar(
            select(Registration.id).where(
                Registration.event_id == event_id,
                Registration.user_id == user_id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
        is not None
    )
    reserved = int(
        await session.scalar(
            select(func.count(WaitlistEntry.id)).where(
                WaitlistEntry.event_id == event_id,
                WaitlistEntry.status == WaitlistStatus.OFFERED,
            )
        )
        or 0
    )
    return event, active, reserved, registered


async def send_event_card(
    message: Message,
    event_id: int,
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        event, active, reserved, registered = await _event_and_state(
            session, event_id, user_id
        )
    if event is None or event.status in (EventStatus.DRAFT, EventStatus.CANCELLED):
        await message.answer("Это мероприятие не найдено или больше недоступно.")
        return
    now = datetime.now(UTC)
    if event.starts_at <= now:
        await message.answer("Это мероприятие уже прошло.")
        return
    text = event_card(event, settings, active=active, reserved=reserved)
    keyboard = event_keyboard(
        event,
        settings,
        registered=registered,
        full=active + reserved >= event.capacity,
    )
    if event.cover_file_id:
        await message.answer_photo(event.cover_file_id, caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await message.answer(
        "Привет! Здесь можно записаться на события сообщества «Резонанс».",
        reply_markup=user_menu(settings),
    )
    if command.args and command.args.startswith("event_"):
        try:
            event_id = int(command.args.removeprefix("event_"))
        except ValueError:
            await message.answer("Ссылка на мероприятие некорректна.")
            return
        await send_event_card(message, event_id, message.from_user.id, session_factory, settings)


@router.message(F.text == "Меню участника")
async def participant_menu(message: Message, settings: Settings) -> None:
    await message.answer("Меню участника", reply_markup=user_menu(settings))


@router.message(F.text == "Написать организатору")
async def contact(message: Message, settings: Settings) -> None:
    await message.answer(
        "Организатор ответит, как только сможет.",
        reply_markup=contact_keyboard(settings),
    )


@router.message(
    (F.text == "Записаться на событие") | (F.text == "Ближайшие события")
)
async def upcoming(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await _send_upcoming(message, session_factory, settings, page=0)


async def _send_upcoming(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    page: int,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        events = list(
            await session.scalars(
                select(Event)
                .where(
                    Event.starts_at > now,
                    Event.status.in_([
                        EventStatus.REGISTRATION_OPEN,
                        EventStatus.REGISTRATION_CLOSED,
                    ]),
                )
                .order_by(Event.starts_at)
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            )
        )
        occupancy: dict[int, tuple[int, int]] = {}
        for event in events[:PAGE_SIZE]:
            active = int(
                await session.scalar(
                    select(func.count(Registration.id)).where(
                        Registration.event_id == event.id,
                        Registration.status == RegistrationStatus.REGISTERED,
                    )
                )
                or 0
            )
            reserved = int(
                await session.scalar(
                    select(func.count(WaitlistEntry.id)).where(
                        WaitlistEntry.event_id == event.id,
                        WaitlistEntry.status == WaitlistStatus.OFFERED,
                    )
                )
                or 0
            )
            occupancy[event.id] = (active, reserved)
    if not events:
        await message.answer("Пока нет ближайших событий.")
        return
    has_next = len(events) > PAGE_SIZE
    builder = InlineKeyboardBuilder()
    for event in events[:PAGE_SIZE]:
        active, reserved = occupancy[event.id]
        if event.status == EventStatus.REGISTRATION_CLOSED:
            state_label = "запись закрыта"
        elif active + reserved >= event.capacity:
            state_label = "лист ожидания"
        else:
            state_label = f"мест: {event.capacity - active}"
        local_date = event.starts_at.astimezone(settings.tz).strftime("%d.%m")
        label = f"{event.title} · {local_date} · {state_label}"
        builder.button(text=label, callback_data=f"event:view:{event.id}")
    if page:
        builder.button(text="← Назад", callback_data=f"events:page:{page - 1}")
    if has_next:
        builder.button(text="Дальше →", callback_data=f"events:page:{page + 1}")
    builder.adjust(1)
    await message.answer(
        "<b>Ближайшие события</b>\n"
        "Выбери событие, чтобы посмотреть подробности и записаться ↓",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("events:page:"))
async def upcoming_page(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await callback.answer()
    page = max(int(callback.data.rsplit(":", 1)[1]), 0)
    await _send_upcoming(callback.message, session_factory, settings, page=page)


@router.callback_query(F.data.startswith("event:view:"))
async def view_event(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    await callback.answer()
    event_id = int(callback.data.rsplit(":", 1)[1])
    await send_event_card(
        callback.message, event_id, callback.from_user.id, session_factory, settings
    )


@router.callback_query(F.data.startswith("event:register:"))
async def register(
    callback: CallbackQuery,
    registration_service: RegistrationService,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    event_id = int(callback.data.rsplit(":", 1)[1])
    outcome = await registration_service.register(event_id, callback.from_user.id)
    await callback.answer()
    async with session_factory() as session:
        event = await session.get(Event, event_id)
    if outcome.result in (RegistrationResult.REGISTERED, RegistrationResult.ALREADY_REGISTERED):
        if event:
            await callback.message.answer(
                registration_success(event, settings),
                reply_markup=event_keyboard(event, settings, registered=True),
            )
    elif outcome.result == RegistrationResult.FULL:
        await callback.message.answer(
            "На это мероприятие сейчас все места заняты.\n"
            "Могу добавить тебя в лист ожидания.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="В лист ожидания", callback_data=f"wait:join:{event_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Другие мероприятия", callback_data="events:page:0"
                        )
                    ],
                ]
            ),
        )
    else:
        await callback.message.answer("Регистрация на это мероприятие уже недоступна.")


@router.callback_query(F.data.startswith("wait:join:"))
async def join_waitlist(
    callback: CallbackQuery,
    registration_service: RegistrationService,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    event_id = int(callback.data.rsplit(":", 1)[1])
    outcome = await registration_service.join_waitlist(event_id, callback.from_user.id)
    await callback.answer()
    if outcome.result in (WaitlistResult.JOINED, WaitlistResult.ALREADY_WAITING):
        await callback.message.answer(
            f"Ты в листе ожидания. Текущая позиция: <b>{outcome.position}</b>.\n"
            "Напишу, если освободится место."
        )
    elif outcome.result == WaitlistResult.PLACE_AVAILABLE:
        outcome_registration = await registration_service.register(event_id, callback.from_user.id)
        async with session_factory() as session:
            event = await session.get(Event, event_id)
        if outcome_registration.result in (
            RegistrationResult.REGISTERED,
            RegistrationResult.ALREADY_REGISTERED,
        ) and event:
            await callback.message.answer(
                registration_success(event, settings),
                reply_markup=event_keyboard(event, settings, registered=True),
            )
        else:
            await callback.message.answer(
                "Место уже заняли. Попробуй вступить в лист ожидания ещё раз."
            )
    else:
        await callback.message.answer("Лист ожидания для этого мероприятия недоступен.")


@router.callback_query(F.data.startswith("cancel:ask:"))
async def ask_cancel(callback: CallbackQuery) -> None:
    event_id = int(callback.data.rsplit(":", 1)[1])
    await callback.answer()
    await callback.message.answer(
        "Точно отменить запись? Освободившееся место может сразу перейти другому участнику.",
        reply_markup=cancel_confirm_keyboard(event_id),
    )


@router.callback_query(F.data.startswith("cancel:no:"))
async def keep_registration(callback: CallbackQuery) -> None:
    await callback.answer("Запись сохранена")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("cancel:yes:"))
async def cancel_registration(
    callback: CallbackQuery,
    registration_service: RegistrationService,
    notifications: NotificationService,
) -> None:
    event_id = int(callback.data.rsplit(":", 1)[1])
    outcome = await registration_service.cancel(event_id, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if outcome.cancelled:
        await callback.message.answer(
            "Спасибо за предупреждение. Ждём тебя в другой раз!"
        )
        if outcome.promotion:
            await notifications.send_promotion(outcome.promotion)
    else:
        await callback.message.answer("Активная запись не найдена.")


@router.callback_query(F.data.startswith("offer:accept:"))
async def accept_offer(
    callback: CallbackQuery,
    registration_service: RegistrationService,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    entry_id = int(callback.data.rsplit(":", 1)[1])
    outcome = await registration_service.accept_offer(entry_id, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if outcome.result in (OfferResult.ACCEPTED, OfferResult.ALREADY_REGISTERED):
        async with session_factory() as session:
            registration = await session.get(Registration, outcome.registration_id)
            event = await session.get(Event, registration.event_id) if registration else None
        if event:
            await callback.message.answer(
                registration_success(event, settings),
                reply_markup=event_keyboard(event, settings, registered=True),
            )
    elif outcome.result == OfferResult.EXPIRED:
        await callback.message.answer("Срок предложения уже истёк.")
    else:
        await callback.message.answer("Это предложение уже недоступно.")
    if outcome.promotion:
        await notifications.send_promotion(outcome.promotion)


@router.callback_query(F.data.startswith("offer:decline:"))
async def decline_offer(
    callback: CallbackQuery,
    registration_service: RegistrationService,
    notifications: NotificationService,
) -> None:
    entry_id = int(callback.data.rsplit(":", 1)[1])
    outcome = await registration_service.decline_offer(entry_id, callback.from_user.id)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Хорошо, предложим место следующему участнику.")
    if outcome.promotion:
        await notifications.send_promotion(outcome.promotion)


@router.message(F.text == "Мои записи")
async def my_registrations(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Registration, Event)
                    .join(Event, Registration.event_id == Event.id)
                    .where(
                        Registration.user_id == message.from_user.id,
                        Registration.status == RegistrationStatus.REGISTERED,
                        Event.starts_at > datetime.now(UTC),
                        Event.status != EventStatus.CANCELLED,
                    )
                    .order_by(Event.starts_at)
                )
            ).all()
        )
    if not rows:
        await message.answer("У тебя пока нет записей на ближайшие события.")
        return
    builder = InlineKeyboardBuilder()
    for _, event in rows:
        builder.button(
            text=f"{event.title} · {human_datetime(event.starts_at, settings.tz)}",
            callback_data=f"event:view:{event.id}",
        )
    builder.adjust(1)
    await message.answer("<b>Мои записи</b>", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("reminder:confirm:"))
async def confirm_reminder(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    event_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session, session.begin():
        registration = await session.scalar(
            select(Registration).where(
                Registration.event_id == event_id,
                Registration.user_id == callback.from_user.id,
                Registration.status == RegistrationStatus.REGISTERED,
            )
        )
        if registration:
            registration.reminder_confirmed_at = datetime.now(UTC)
    await callback.answer("Отлично, ждём тебя!")


@router.callback_query(F.data.startswith("feedback:absent:"))
async def feedback_absent(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registration_id = int(callback.data.split(":")[2])
    async with session_factory() as session, session.begin():
        registration = await session.get(Registration, registration_id)
        if registration is None or registration.user_id != callback.from_user.id:
            await callback.answer("Ответ не найден", show_alert=True)
            return
        feedback = await session.scalar(
            select(Feedback).where(Feedback.registration_id == registration_id)
        )
        if feedback is None:
            session.add(Feedback(registration_id=registration_id, did_not_attend=True))
        else:
            feedback.did_not_attend = True
            feedback.rating = None
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Спасибо за ответ!")


@router.callback_query(F.data.startswith("feedback:rate:"))
async def feedback_rate(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, registration_raw, rating_raw = callback.data.split(":")
    registration_id, rating = int(registration_raw), int(rating_raw)
    async with session_factory() as session, session.begin():
        registration = await session.get(Registration, registration_id)
        if registration is None or registration.user_id != callback.from_user.id:
            await callback.answer("Ответ не найден", show_alert=True)
            return
        feedback = await session.scalar(
            select(Feedback).where(Feedback.registration_id == registration_id)
        )
        if feedback is None:
            session.add(Feedback(registration_id=registration_id, rating=rating))
        else:
            feedback.rating = rating
            feedback.did_not_attend = False
    await state.set_state(FeedbackComment.text)
    await state.update_data(registration_id=registration_id)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Можешь по желанию оставить комментарий: что понравилось, а что можно улучшить.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Пропустить", callback_data="feedback:skip")]
            ]
        ),
    )


@router.callback_query(FeedbackComment.text, F.data == "feedback:skip")
async def feedback_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Спасибо за отзыв!")


@router.message(FeedbackComment.text)
async def feedback_comment(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    data = await state.get_data()
    registration_id = int(data["registration_id"])
    async with session_factory() as session, session.begin():
        feedback = await session.scalar(
            select(Feedback).where(Feedback.registration_id == registration_id)
        )
        if feedback:
            feedback.comment = (message.text or "")[:4000]
    await state.clear()
    await message.answer("Спасибо за отзыв!")
