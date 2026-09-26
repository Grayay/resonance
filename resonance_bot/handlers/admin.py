from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from resonance_bot.config import Settings
from resonance_bot.db.models import (
    Admin,
    Event,
    EventStatus,
    Feedback,
    JobStatus,
    Registration,
    RegistrationStatus,
    Reminder,
    User,
    WaitlistEntry,
    WaitlistStatus,
)
from resonance_bot.keyboards import admin_event_keyboard, admin_menu
from resonance_bot.services.events import event_stats, reschedule_registration_jobs
from resonance_bot.services.exports import event_csv
from resonance_bot.services.notifications import NotificationService
from resonance_bot.services.users import is_admin
from resonance_bot.states import AdminManagement, Broadcast, EventCreation, EventEdit
from resonance_bot.utils.dates import combine_local, human_datetime, parse_date, parse_time
from resonance_bot.utils.formatting import (
    STATUS_LABELS,
    admin_event_card,
    event_announcement,
    event_deep_link,
    event_schedule_cta,
)

router = Router(name="admin")


async def _authorized(
    user_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    async with session_factory() as session:
        return await is_admin(session, user_id, settings.superadmin_id)


async def _guard_message(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    allowed = await _authorized(message.from_user.id, session_factory, settings)
    if not allowed:
        await message.answer("Эта команда доступна только администраторам.")
    return allowed


async def _guard_callback(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    allowed = await _authorized(callback.from_user.id, session_factory, settings)
    if not allowed:
        await callback.answer("Недостаточно прав", show_alert=True)
    return allowed


@router.message(Command("admin"))
async def admin_command(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_message(message, session_factory, settings):
        return
    await message.answer(
        "<b>Панель администратора</b>",
        reply_markup=admin_menu(superadmin=message.from_user.id == settings.superadmin_id),
    )


@router.message(Command("cancel"))
async def cancel_admin_flow(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нет активного действия для отмены.")
        return
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(F.text == "Создать мероприятие")
async def create_event_start(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_message(message, session_factory, settings):
        return
    await state.clear()
    await state.set_state(EventCreation.title)
    await message.answer("Введи название мероприятия:")


async def _after_value(
    message: Message,
    state: FSMContext,
    next_state,
    next_prompt: str,
    settings: Settings,
) -> None:
    data = await state.get_data()
    if data.get("editing_preview"):
        await state.update_data(editing_preview=False)
        await _show_creation_preview(message, state, settings)
        return
    await state.set_state(next_state)
    await message.answer(next_prompt)


@router.message(EventCreation.title)
async def create_title(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.text or len(message.text.strip()) > 255:
        await message.answer("Название должно быть текстом до 255 символов.")
        return
    await state.update_data(title=message.text.strip())
    await _after_value(
        message,
        state,
        EventCreation.description,
        "Введи текст анонса. Я не буду переписывать его, только аккуратно отформатирую:",
        settings,
    )


@router.message(EventCreation.description)
async def create_description(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.text or len(message.text.strip()) > 3500:
        await message.answer("Текст должен содержать от 1 до 3500 символов.")
        return
    await state.update_data(description=message.text.strip())
    await _after_value(
        message,
        state,
        EventCreation.event_date,
        "Дата мероприятия в формате ДД.ММ.ГГГГ:",
        settings,
    )


@router.message(EventCreation.event_date)
async def create_date(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        day = parse_date(message.text or "")
    except ValueError:
        await message.answer("Не получилось прочитать дату. Используй формат ДД.ММ.ГГГГ.")
        return
    if day < datetime.now(settings.tz).date():
        await message.answer("Дата уже прошла. Введи будущую дату.")
        return
    await state.update_data(event_date=day.isoformat())
    await _after_value(
        message, state, EventCreation.start_time, "Время начала в формате ЧЧ:ММ:", settings
    )


@router.message(EventCreation.start_time)
async def create_time(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        clock = parse_time(message.text or "")
    except ValueError:
        await message.answer("Не получилось прочитать время. Используй формат ЧЧ:ММ.")
        return
    await state.update_data(start_time=clock.strftime("%H:%M"))
    await _after_value(
        message, state, EventCreation.venue_name, "Название площадки:", settings
    )


@router.message(EventCreation.venue_name)
async def create_venue(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.text:
        await message.answer("Введи название площадки.")
        return
    await state.update_data(venue_name=message.text.strip()[:255])
    await _after_value(message, state, EventCreation.address, "Адрес:", settings)


@router.message(EventCreation.address)
async def create_address(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.text:
        await message.answer("Введи адрес.")
        return
    await state.update_data(address=message.text.strip()[:500])
    await _after_value(
        message, state, EventCreation.map_url, "Ссылка на точку в картах:", settings
    )


@router.message(EventCreation.map_url)
async def create_map(message: Message, state: FSMContext, settings: Settings) -> None:
    value = (message.text or "").strip()
    if not value.startswith(("https://", "http://")):
        await message.answer("Нужна полная ссылка, начинающаяся с https:// или http://.")
        return
    await state.update_data(map_url=value[:1000])
    await _after_value(
        message,
        state,
        EventCreation.cover,
        "Отправь обложку как фото или напиши «Пропустить»:",
        settings,
    )


@router.message(EventCreation.cover)
async def create_cover(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.photo:
        cover_file_id = message.photo[-1].file_id
    elif (message.text or "").strip().lower() == "пропустить":
        cover_file_id = None
    else:
        await message.answer("Отправь фото или напиши «Пропустить».")
        return
    await state.update_data(cover_file_id=cover_file_id)
    await _after_value(
        message, state, EventCreation.capacity, "Количество мест (целое число):", settings
    )


@router.message(EventCreation.capacity)
async def create_capacity(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        capacity = int(message.text or "")
        if not 1 <= capacity <= 100_000:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число от 1 до 100000.")
        return
    await state.update_data(capacity=capacity)
    await _after_value(
        message,
        state,
        EventCreation.reminders,
        "За сколько часов напомнить? Например: 24, 3. Напиши «По умолчанию» для настроек из .env:",
        settings,
    )


@router.message(EventCreation.reminders)
async def create_reminders(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = (message.text or "").strip().lower()
    try:
        offsets = (
            settings.default_reminder_hours
            if raw == "по умолчанию"
            else sorted({int(part.strip()) for part in raw.split(",")}, reverse=True)
        )
        if not offsets or any(offset < 1 or offset > 24 * 30 for offset in offsets):
            raise ValueError
    except ValueError:
        await message.answer("Укажи часы через запятую, например: 24, 3.")
        return
    await state.update_data(reminder_offsets_hours=offsets)
    await _after_value(
        message,
        state,
        EventCreation.feedback_delay,
        "Через сколько часов после начала запросить отзыв? Введи число или «Пропустить»:",
        settings,
    )


@router.message(EventCreation.feedback_delay)
async def create_feedback(message: Message, state: FSMContext, settings: Settings) -> None:
    raw = (message.text or "").strip().lower()
    if raw == "пропустить":
        delay = None
    else:
        try:
            delay = int(raw)
            if not 1 <= delay <= 24 * 30:
                raise ValueError
        except ValueError:
            await message.answer("Введи число часов или «Пропустить».")
            return
    await state.update_data(feedback_delay_hours=delay)
    await _show_creation_preview(message, state, settings)


def _creation_preview_text(data: dict, settings: Settings) -> str:
    day = datetime.fromisoformat(data["event_date"]).date()
    clock = parse_time(data["start_time"])
    starts_at = combine_local(day, clock, settings.tz)
    reminders = ", ".join(str(value) for value in data["reminder_offsets_hours"])
    feedback = (
        f"через {data['feedback_delay_hours']} ч."
        if data.get("feedback_delay_hours")
        else "выключен"
    )
    return (
        "<b>Предпросмотр</b>\n\n"
        f"<b>{escape(data['title'])}</b>\n\n"
        f"{escape(data['description'])}\n\n"
        f"📅 {human_datetime(starts_at, settings.tz)}\n"
        f"📍 {escape(data['venue_name'])}, {escape(data['address'])}\n"
        f"Мест: {data['capacity']}\n"
        f"Напоминания: за {reminders} ч.\n"
        f"Отзыв: {feedback}"
    )


async def _show_creation_preview(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    await state.set_state(EventCreation.preview)
    data = await state.get_data()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data="create:publish")],
            [InlineKeyboardButton(text="Сохранить без публикации", callback_data="create:save")],
            [InlineKeyboardButton(text="Редактировать", callback_data="create:edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="create:cancel")],
        ]
    )
    text = _creation_preview_text(data, settings)
    if data.get("cover_file_id"):
        await message.answer_photo(data["cover_file_id"], caption=text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


CREATE_FIELDS = {
    "title": ("Название", EventCreation.title),
    "description": ("Текст анонса", EventCreation.description),
    "event_date": ("Дата", EventCreation.event_date),
    "start_time": ("Время", EventCreation.start_time),
    "venue_name": ("Площадка", EventCreation.venue_name),
    "address": ("Адрес", EventCreation.address),
    "map_url": ("Ссылка на карту", EventCreation.map_url),
    "cover_file_id": ("Обложка", EventCreation.cover),
    "capacity": ("Количество мест", EventCreation.capacity),
    "reminder_offsets_hours": ("Напоминания", EventCreation.reminders),
    "feedback_delay_hours": ("Запрос отзыва", EventCreation.feedback_delay),
}


@router.callback_query(EventCreation.preview, F.data == "create:edit")
async def creation_edit(callback: CallbackQuery) -> None:
    builder = InlineKeyboardBuilder()
    for field, (label, _) in CREATE_FIELDS.items():
        builder.button(text=label, callback_data=f"create:field:{field}")
    builder.adjust(2)
    await callback.answer()
    await callback.message.answer("Что изменить?", reply_markup=builder.as_markup())


@router.callback_query(EventCreation.preview, F.data.startswith("create:field:"))
async def creation_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.rsplit(":", 1)[1]
    definition = CREATE_FIELDS.get(field)
    if not definition:
        await callback.answer("Неизвестное поле")
        return
    label, target_state = definition
    await state.update_data(editing_preview=True)
    await state.set_state(target_state)
    await callback.answer()
    await callback.message.answer(f"Введи новое значение поля «{label}»:")


@router.callback_query(EventCreation.preview, F.data == "create:cancel")
async def creation_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer("Создание мероприятия отменено.")


async def _save_created_event(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
    *,
    publish: bool,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    data = await state.get_data()
    day = datetime.fromisoformat(data["event_date"]).date()
    starts_at = combine_local(day, parse_time(data["start_time"]), settings.tz)
    if starts_at <= datetime.now(UTC):
        await callback.answer("Дата и время уже прошли", show_alert=True)
        return
    feedback_at = (
        starts_at + timedelta(hours=data["feedback_delay_hours"])
        if data.get("feedback_delay_hours")
        else None
    )
    async with session_factory() as session, session.begin():
        event = Event(
            title=data["title"],
            description=data["description"],
            starts_at=starts_at,
            venue_name=data["venue_name"],
            address=data["address"],
            map_url=data["map_url"],
            cover_file_id=data.get("cover_file_id"),
            capacity=data["capacity"],
            status=EventStatus.DRAFT,
            reminder_offsets_hours=data["reminder_offsets_hours"],
            feedback_at=feedback_at,
            created_by=callback.from_user.id,
        )
        session.add(event)
        await session.flush()
        event_id = event.id
    await state.clear()
    await callback.answer()
    if publish:
        try:
            await notifications.publish_event(event_id)
        except Exception:
            await callback.message.answer(
                "Мероприятие сохранено как черновик, но публикация не удалась. "
                "Проверь права бота в канале и настройки CHANNEL_ID."
            )
            return
        await callback.message.answer("Мероприятие опубликовано, регистрация открыта.")
    else:
        await callback.message.answer(f"Мероприятие сохранено как черновик. ID: {event_id}")


@router.callback_query(EventCreation.preview, F.data == "create:save")
async def creation_save(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    await _save_created_event(
        callback, state, session_factory, settings, notifications, publish=False
    )


@router.callback_query(EventCreation.preview, F.data == "create:publish")
async def creation_publish(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    await _save_created_event(
        callback, state, session_factory, settings, notifications, publish=True
    )


@router.message(F.text == "Мероприятия")
async def admin_events(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_message(message, session_factory, settings):
        return
    async with session_factory() as session:
        events = list(
            await session.scalars(
                select(Event).order_by(Event.starts_at.desc()).limit(30)
            )
        )
    if not events:
        await message.answer("Мероприятий пока нет.")
        return
    builder = InlineKeyboardBuilder()
    for event in events:
        builder.button(
            text=f"{event.title} · {STATUS_LABELS[event.status]}",
            callback_data=f"admin:event:{event.id}:view",
        )
    builder.adjust(1)
    await message.answer("<b>Мероприятия</b>", reply_markup=builder.as_markup())


@router.callback_query(F.data.regexp(r"^admin:event:\d+:view$"))
async def admin_view_event(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        event = await session.get(Event, event_id)
        active = int(
            await session.scalar(
                select(func.count(Registration.id)).where(
                    Registration.event_id == event_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                )
            )
            or 0
        )
    await callback.answer()
    if not event:
        await callback.message.answer("Мероприятие не найдено.")
        return
    await callback.message.answer(
        admin_event_card(event, settings, active=active),
        reply_markup=admin_event_keyboard(event),
    )


@router.callback_query(F.data.regexp(r"^admin:event:\d+:participants$"))
async def admin_participants(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        event = await session.get(Event, event_id)
        rows = list(
            (
                await session.execute(
                    select(Registration, User, Feedback)
                    .join(User, User.telegram_user_id == Registration.user_id)
                    .outerjoin(Feedback, Feedback.registration_id == Registration.id)
                    .where(Registration.event_id == event_id)
                    .order_by(Registration.registered_at)
                    .limit(100)
                )
            ).all()
        )
        waiting = int(
            await session.scalar(
                select(func.count(WaitlistEntry.id)).where(
                    WaitlistEntry.event_id == event_id,
                    WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
                )
            )
            or 0
        )
        waitlist_rows = list(
            (
                await session.execute(
                    select(WaitlistEntry, User)
                    .join(User, User.telegram_user_id == WaitlistEntry.user_id)
                    .where(WaitlistEntry.event_id == event_id)
                    .order_by(WaitlistEntry.joined_at)
                    .limit(50)
                )
            ).all()
        )
    await callback.answer()
    if not event:
        await callback.message.answer("Мероприятие не найдено.")
        return
    active = sum(reg.status == RegistrationStatus.REGISTERED for reg, _, _ in rows)
    cancelled = sum(reg.status == RegistrationStatus.CANCELLED for reg, _, _ in rows)
    confirmed = sum(bool(reg.reminder_confirmed_at) for reg, _, _ in rows)
    feedback_count = sum(feedback is not None for _, _, feedback in rows)
    lines = [
        f"<b>{escape(event.title)} — участники</b>",
        f"Записаны: {active} · Отменили: {cancelled} · Лист ожидания: {waiting}",
        f"Подтвердили напоминание: {confirmed} · Отзывы: {feedback_count}",
        "",
    ]
    for registration, user, feedback in rows[:50]:
        status = "записан" if registration.status == RegistrationStatus.REGISTERED else "отменил"
        flags = []
        if registration.reminder_confirmed_at:
            flags.append("подтвердил")
        if feedback:
            flags.append("оставил отзыв")
        username = f"@{escape(user.username)}" if user.username else f"ID {user.telegram_user_id}"
        name = escape(" ".join(filter(None, [user.first_name, user.last_name])))
        suffix = f"; {', '.join(flags)}" if flags else ""
        lines.append(f"• {name} ({username}) — {status}{suffix}")
    if len(rows) > 50:
        lines.append("\nПоказаны первые 50 записей. Полный список доступен в CSV.")
    if waitlist_rows:
        waitlist_labels = {
            WaitlistStatus.WAITING: "в листе ожидания",
            WaitlistStatus.OFFERED: "место предложено",
            WaitlistStatus.ACCEPTED: "принял место",
            WaitlistStatus.DECLINED: "отказался от места",
            WaitlistStatus.EXPIRED: "предложение истекло",
            WaitlistStatus.CANCELLED: "ожидание отменено",
        }
        lines.append("\n<b>Лист ожидания</b>")
        for entry, user in waitlist_rows:
            name = escape(" ".join(filter(None, [user.first_name, user.last_name])))
            username = (
                f"@{escape(user.username)}"
                if user.username
                else f"ID {user.telegram_user_id}"
            )
            lines.append(f"• {name} ({username}) — {waitlist_labels[entry.status]}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data.regexp(r"^admin:event:\d+:stats$"))
async def admin_stats(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        event = await session.get(Event, event_id)
        stats = await event_stats(session, event) if event else None
    await callback.answer()
    if not event or not stats:
        await callback.message.answer("Мероприятие не найдено.")
        return
    average = f"{stats.average_rating:.2f}" if stats.average_rating is not None else "—"
    await callback.message.answer(
        f"<b>Статистика: {escape(event.title)}</b>\n\n"
        f"Вместимость: {stats.capacity}\n"
        f"Активные записи: {stats.active}\n"
        f"Свободные места: {stats.free}\n"
        f"Лист ожидания: {stats.waitlist}\n"
        f"Отмены: {stats.cancellations}\n"
        f"Подтверждения в напоминании: {stats.confirmations}\n"
        f"Отзывы: {stats.feedback_count}\n"
        f"Средняя оценка: {average}"
    )


@router.callback_query(F.data.regexp(r"^admin:event:\d+:export$"))
async def admin_export(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        data = await event_csv(session, event_id)
    await callback.answer()
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"event-{event_id}-registrations.csv"),
        caption="Экспорт регистраций в CSV (UTF-8).",
    )


@router.callback_query(F.data.regexp(r"^admin:event:\d+:manual$"))
async def admin_manual_publication(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id, with_for_update=True)
        if event and event.status == EventStatus.CANCELLED:
            await callback.answer("Отменённое мероприятие недоступно", show_alert=True)
            return
        if event and event.starts_at <= datetime.now(UTC):
            await callback.answer("Прошедшее мероприятие недоступно", show_alert=True)
            return
        if event and event.published_at is None:
            event.status = EventStatus.REGISTRATION_OPEN
            event.published_at = datetime.now(UTC)
    await callback.answer()
    if not event:
        await callback.message.answer("Мероприятие не найдено.")
        return
    link = event_deep_link(event.id, settings)
    registration_state = (
        "Регистрация открыта."
        if event.status == EventStatus.REGISTRATION_OPEN
        else "Сейчас регистрация закрыта."
    )
    intro = (
        f"<b>Ссылка для ручной публикации</b>\n<code>{escape(link)}</code>\n\n"
        f"Короткий призыв:\n<code>Записаться на «{escape(event.title)}»: "
        f"{escape(link)}</code>\n\n"
        + registration_state
    )
    announcement = event_announcement(event, settings)
    if len(intro) + len(announcement) < 3900:
        await callback.message.answer(f"{intro}\n\n<b>Готовый анонс</b>\n\n{announcement}")
    else:
        await callback.message.answer(intro)
        await callback.message.answer(
            f"<b>{escape(event.title)}</b>\n\n{escape(event.description)}"
        )
        await callback.message.answer(event_schedule_cta(event, settings))


@router.callback_query(F.data.regexp(r"^admin:event:\d+:publish$"))
async def admin_publish(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    await callback.answer()
    try:
        await notifications.publish_event(event_id)
    except Exception:
        await callback.message.answer(
            "Не удалось опубликовать. Проверь права бота в канале и настройки CHANNEL_ID."
        )
        return
    await callback.message.answer("Мероприятие опубликовано, регистрация открыта.")


@router.callback_query(F.data.regexp(r"^admin:event:\d+:close$"))
async def admin_close_registration(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id, with_for_update=True)
        if event and event.status == EventStatus.REGISTRATION_OPEN:
            event.status = EventStatus.REGISTRATION_CLOSED
            await session.execute(
                update(WaitlistEntry)
                .where(
                    WaitlistEntry.event_id == event_id,
                    WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
                )
                .values(status=WaitlistStatus.CANCELLED)
            )
    await notifications.disable_channel_registration(event_id)
    await callback.answer()
    await callback.message.answer("Регистрация закрыта.")


@router.callback_query(F.data.regexp(r"^admin:event:\d+:cancel$"))
async def admin_ask_cancel_event(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    await callback.answer()
    await callback.message.answer(
        "Точно отменить мероприятие? Все записанные участники получат уведомление.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, отменить", callback_data=f"admin:cancelconfirm:{event_id}"
                    )
                ],
                [InlineKeyboardButton(text="Нет", callback_data="admin:cancelabort")],
            ]
        ),
    )


@router.callback_query(F.data == "admin:cancelabort")
async def admin_abort_cancel_event(callback: CallbackQuery) -> None:
    await callback.answer("Отмена не выполнена")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.regexp(r"^admin:cancelconfirm:\d+$"))
async def admin_cancel_event(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id, with_for_update=True)
        if not event:
            await callback.answer("Мероприятие не найдено", show_alert=True)
            return
        event.status = EventStatus.CANCELLED
        await session.execute(
            update(Reminder)
            .where(
                Reminder.registration_id.in_(
                    select(Registration.id).where(Registration.event_id == event_id)
                ),
                Reminder.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
            )
            .values(status=JobStatus.SKIPPED)
        )
        await session.execute(
            update(WaitlistEntry)
            .where(
                WaitlistEntry.event_id == event_id,
                WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
            )
            .values(status=WaitlistStatus.CANCELLED)
        )
        title = event.title
    participant_ids = await notifications.event_participant_ids(event_id)
    await notifications.broadcast(
        participant_ids,
        f"Мероприятие <b>{escape(title)}</b> отменено. "
        "Если появится новая дата, мы сообщим отдельно.",
        created_by=callback.from_user.id,
        event_id=event_id,
    )
    await notifications.disable_channel_registration(event_id)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Мероприятие отменено, участники уведомлены.")


EDIT_FIELDS = {
    "title": "Название",
    "description": "Текст анонса",
    "date": "Дата",
    "time": "Время",
    "venue_name": "Площадка",
    "address": "Адрес",
    "map_url": "Ссылка на карту",
    "cover_file_id": "Обложка",
    "capacity": "Количество мест",
    "reminders": "Напоминания",
    "feedback": "Запрос отзыва",
}
IMPORTANT_EDIT_FIELDS = {"date", "time", "venue_name", "address", "map_url"}


@router.callback_query(F.data.regexp(r"^admin:event:\d+:edit$"))
async def admin_edit_event(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    builder = InlineKeyboardBuilder()
    for field, label in EDIT_FIELDS.items():
        builder.button(text=label, callback_data=f"admin:editfield:{event_id}:{field}")
    builder.adjust(2)
    await state.set_state(EventEdit.choosing)
    await callback.answer()
    await callback.message.answer("Что изменить?", reply_markup=builder.as_markup())


@router.callback_query(EventEdit.choosing, F.data.startswith("admin:editfield:"))
async def admin_choose_edit_field(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    _, _, event_raw, field = callback.data.split(":")
    event_id = int(event_raw)
    async with session_factory() as session:
        event = await session.get(Event, event_id)
    if not event:
        await callback.answer("Мероприятие не найдено", show_alert=True)
        return
    if field == "capacity" and event.published_at is not None:
        await callback.answer(
            "В MVP количество мест после публикации не меняется", show_alert=True
        )
        return
    prompts = {
        "title": "Новое название:",
        "description": "Новый текст анонса:",
        "date": "Новая дата в формате ДД.ММ.ГГГГ:",
        "time": "Новое время в формате ЧЧ:ММ:",
        "venue_name": "Новое название площадки:",
        "address": "Новый адрес:",
        "map_url": "Новая ссылка на карту:",
        "cover_file_id": "Отправь новое фото или напиши «Удалить»:",
        "capacity": "Новое количество мест:",
        "reminders": "Новые отступы напоминаний в часах, через запятую:",
        "feedback": "Часов после начала до запроса отзыва или «Отключить»:",
    }
    await state.set_state(EventEdit.value)
    await state.update_data(event_id=event_id, field=field)
    await callback.answer()
    await callback.message.answer(prompts[field])


@router.message(EventEdit.value)
async def admin_save_edit(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_message(message, session_factory, settings):
        return
    data = await state.get_data()
    event_id, field = int(data["event_id"]), data["field"]
    raw = (message.text or "").strip()
    important = field in IMPORTANT_EDIT_FIELDS
    async with session_factory() as session, session.begin():
        event = await session.get(Event, event_id, with_for_update=True)
        if not event:
            await state.clear()
            await message.answer("Мероприятие не найдено.")
            return
        feedback_delay = (
            event.feedback_at - event.starts_at if event.feedback_at is not None else None
        )
        try:
            if field == "title":
                if not raw or len(raw) > 255:
                    raise ValueError("Название должно содержать до 255 символов")
                event.title = raw
            elif field == "description":
                if not raw or len(raw) > 3500:
                    raise ValueError("Текст должен содержать до 3500 символов")
                event.description = raw
            elif field == "date":
                local = event.starts_at.astimezone(settings.tz)
                event.starts_at = combine_local(parse_date(raw), local.time(), settings.tz)
            elif field == "time":
                local = event.starts_at.astimezone(settings.tz)
                event.starts_at = combine_local(local.date(), parse_time(raw), settings.tz)
            elif field == "venue_name":
                if not raw:
                    raise ValueError("Площадка не может быть пустой")
                event.venue_name = raw[:255]
            elif field == "address":
                if not raw:
                    raise ValueError("Адрес не может быть пустым")
                event.address = raw[:500]
            elif field == "map_url":
                if not raw.startswith(("https://", "http://")):
                    raise ValueError("Ссылка должна начинаться с https:// или http://")
                event.map_url = raw[:1000]
            elif field == "cover_file_id":
                if message.photo:
                    event.cover_file_id = message.photo[-1].file_id
                elif raw.lower() == "удалить":
                    event.cover_file_id = None
                else:
                    raise ValueError("Отправь фото или напиши «Удалить»")
            elif field == "capacity":
                capacity = int(raw)
                active = int(
                    await session.scalar(
                        select(func.count(Registration.id)).where(
                            Registration.event_id == event_id,
                            Registration.status == RegistrationStatus.REGISTERED,
                        )
                    )
                    or 0
                )
                if capacity < max(active, 1):
                    raise ValueError("Количество мест не может быть меньше числа записанных")
                event.capacity = capacity
            elif field == "reminders":
                offsets = sorted({int(part.strip()) for part in raw.split(",")}, reverse=True)
                if not offsets or any(value < 1 or value > 720 for value in offsets):
                    raise ValueError("Укажи значения от 1 до 720 часов")
                event.reminder_offsets_hours = offsets
            elif field == "feedback":
                if raw.lower() == "отключить":
                    event.feedback_at = None
                else:
                    delay = int(raw)
                    if not 1 <= delay <= 720:
                        raise ValueError("Укажи значение от 1 до 720 часов")
                    event.feedback_at = event.starts_at + timedelta(hours=delay)
            else:
                raise ValueError("Неизвестное поле")
            if field in {"date", "time"} and feedback_delay is not None:
                event.feedback_at = event.starts_at + feedback_delay
            if event.starts_at <= datetime.now(UTC):
                raise ValueError("Дата и время должны быть в будущем")
            if field in {"date", "time", "reminders", "feedback"}:
                await reschedule_registration_jobs(session, event)
        except (ValueError, TypeError):
            await session.rollback()
            await message.answer(
                "Не удалось сохранить. Проверь формат значения и попробуй ещё раз."
            )
            return
    await state.clear()
    if important:
        await message.answer(
            "Изменение сохранено. Уведомить записанных участников?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Да, уведомить",
                            callback_data=f"admin:notifychange:{event_id}",
                        )
                    ],
                    [InlineKeyboardButton(text="Нет", callback_data="admin:notifychange:no")],
                ]
            ),
        )
    else:
        await message.answer("Изменение сохранено.")


@router.callback_query(F.data == "admin:notifychange:no")
async def admin_skip_change_notification(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Участники не уведомлены.")


@router.callback_query(F.data.regexp(r"^admin:notifychange:\d+$"))
async def admin_notify_change(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session:
        event = await session.get(Event, event_id)
    if not event:
        await callback.answer("Мероприятие не найдено", show_alert=True)
        return
    ids = await notifications.event_participant_ids(event_id)
    summary = await notifications.broadcast(
        ids,
        f"Обновились детали мероприятия <b>{escape(event.title)}</b>.\n\n"
        f"Новые данные: {human_datetime(event.starts_at, settings.tz)}\n"
        f"{escape(event.venue_name)}, {escape(event.address)}",
        created_by=callback.from_user.id,
        event_id=event_id,
    )
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Уведомление доставлено: {summary.delivered}. Ошибок: {summary.failed}."
    )


@router.message(F.text == "Рассылка")
async def global_broadcast_start(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_message(message, session_factory, settings):
        return
    await state.set_state(Broadcast.text)
    await state.update_data(event_id=None)
    await message.answer("Введи текст рассылки всем пользователям:")


@router.callback_query(F.data.regexp(r"^admin:event:\d+:broadcast$"))
async def event_broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    event_id = int(callback.data.split(":")[2])
    await state.set_state(Broadcast.text)
    await state.update_data(event_id=event_id)
    await callback.answer()
    await callback.message.answer("Введи текст для активных участников мероприятия:")


@router.message(Broadcast.text)
async def broadcast_preview(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > 4000:
        await message.answer("Текст должен содержать от 1 до 4000 символов.")
        return
    await state.update_data(text=text)
    await state.set_state(Broadcast.preview)
    await message.answer(
        f"<b>Предпросмотр рассылки</b>\n\n{escape(text)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отправить", callback_data="broadcast:confirm")],
                [InlineKeyboardButton(text="Отмена", callback_data="broadcast:cancel")],
            ]
        ),
    )


@router.callback_query(Broadcast.preview, F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer("Рассылка отменена.")


@router.callback_query(Broadcast.preview, F.data == "broadcast:confirm")
async def broadcast_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifications: NotificationService,
) -> None:
    if not await _guard_callback(callback, session_factory, settings):
        return
    data = await state.get_data()
    await state.clear()
    await callback.answer()
    await callback.message.answer("Рассылка началась.")
    event_id = data.get("event_id")
    ids = (
        await notifications.event_participant_ids(event_id)
        if event_id
        else await notifications.all_user_ids()
    )
    summary = await notifications.broadcast(
        ids,
        escape(data["text"]),
        created_by=callback.from_user.id,
        event_id=event_id,
    )
    await callback.message.answer(
        f"Рассылка завершена. Доставлено: {summary.delivered} из {summary.total}. "
        f"Ошибок: {summary.failed}."
    )


@router.message(F.text == "Администраторы")
async def admin_management(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("Управлять администраторами может только суперадминистратор.")
        return
    async with session_factory() as session:
        admins = list(
            (
                await session.execute(
                    select(Admin, User)
                    .join(User, User.telegram_user_id == Admin.telegram_user_id)
                    .order_by(Admin.created_at)
                )
            ).all()
        )
    lines = [f"• {escape(user.first_name)} — {user.telegram_user_id}" for _, user in admins]
    text = "<b>Администраторы</b>\n" + ("\n".join(lines) if lines else "Пока никого нет.")
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Добавить", callback_data="admins:add")],
                [InlineKeyboardButton(text="Удалить", callback_data="admins:remove")],
            ]
        ),
    )


@router.callback_query(F.data.in_({"admins:add", "admins:remove"}))
async def admin_management_choice(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    adding = callback.data == "admins:add"
    await state.set_state(AdminManagement.add if adding else AdminManagement.remove)
    await callback.answer()
    await callback.message.answer(
        "Введи Telegram ID пользователя. Пользователь должен хотя бы раз открыть бота:"
    )


@router.message(AdminManagement.add)
async def add_admin(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    try:
        user_id = int(message.text or "")
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return
    async with session_factory() as session, session.begin():
        user = await session.get(User, user_id)
        if not user:
            await message.answer("Пользователь ещё не открывал бота, поэтому добавить его нельзя.")
            return
        exists = await session.scalar(select(Admin.id).where(Admin.telegram_user_id == user_id))
        if exists:
            await message.answer("Этот пользователь уже администратор.")
            await state.clear()
            return
        session.add(Admin(telegram_user_id=user_id, added_by=message.from_user.id))
    await state.clear()
    await message.answer("Администратор добавлен.")


@router.message(AdminManagement.remove)
async def remove_admin(
    message: Message,
    state: FSMContext,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    try:
        user_id = int(message.text or "")
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return
    if user_id == settings.superadmin_id:
        await message.answer("Суперадминистратор задаётся в .env и не может удалить себя.")
        await state.clear()
        return
    async with session_factory() as session, session.begin():
        result = await session.execute(
            select(Admin).where(Admin.telegram_user_id == user_id).with_for_update()
        )
        admin = result.scalar_one_or_none()
        if admin:
            await session.delete(admin)
    await state.clear()
    await message.answer("Администратор удалён." if admin else "Администратор не найден.")
