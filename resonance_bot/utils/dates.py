from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_time(value: str) -> time:
    return datetime.strptime(value.strip(), "%H:%M").time()


def combine_local(day: date, clock: time, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, timezone).astimezone(UTC)


def localize(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone)


def human_date(value: datetime, timezone: ZoneInfo) -> str:
    local = localize(value, timezone)
    return f"{local.day} {RU_MONTHS[local.month - 1]} {local.year}"


def human_time(value: datetime, timezone: ZoneInfo) -> str:
    return localize(value, timezone).strftime("%H:%M")


def human_datetime(value: datetime, timezone: ZoneInfo) -> str:
    return f"{human_date(value, timezone)}, {human_time(value, timezone)}"

