from aiogram.fsm.state import State, StatesGroup


class EventCreation(StatesGroup):
    channel = State()
    title = State()
    description = State()
    event_type = State()
    price = State()
    payment_details = State()
    event_date = State()
    start_time = State()
    venue_name = State()
    address = State()
    map_url = State()
    cover = State()
    capacity = State()
    reminders = State()
    feedback_delay = State()
    preview = State()


class EventEdit(StatesGroup):
    choosing = State()
    value = State()


class Broadcast(StatesGroup):
    text = State()
    preview = State()


class AdminManagement(StatesGroup):
    add = State()
    remove = State()


class FeedbackComment(StatesGroup):
    text = State()


class ReceiptSelection(StatesGroup):
    event = State()
