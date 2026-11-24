from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, PositiveInt, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    superadmin_ids: Annotated[set[PositiveInt], NoDecode] = Field(
        validation_alias=AliasChoices("superadmin_ids", "superadmin_id"),
        min_length=1,
    )
    database_url: str
    main_channel_id: str = Field(
        validation_alias=AliasChoices("main_channel_id", "channel_id"),
        min_length=1,
    )
    test_channel_id: str | None = None
    community_contact: str
    payment_details: str = ""
    timezone: str = "Europe/Moscow"
    bot_username: str
    waitlist_offer_ttl_minutes: int = Field(default=120, ge=5, le=1440)
    scheduler_poll_seconds: int = Field(default=20, ge=5, le=300)
    default_reminder_hours: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [24, 3])
    log_level: str = "INFO"

    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def parse_superadmins(cls, value):
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if any(not part.isascii() or not part.isdecimal() for part in parts):
                raise ValueError(
                    "SUPERADMIN_IDS must contain comma-separated positive Telegram IDs"
                )
            return [int(part) for part in parts]
        if type(value) is int:
            return [value]
        return value

    @field_validator("main_channel_id", "test_channel_id", mode="before")
    @classmethod
    def strip_channel(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    def channel_id_for(self, target: str) -> str:
        if target == "main":
            return self.main_channel_id
        if target == "test":
            if self.test_channel_id:
                return self.test_channel_id
            raise ValueError("Тестовый канал не настроен: укажите TEST_CHANNEL_ID")
        raise ValueError("Неизвестный канал публикации")

    @field_validator("bot_username", mode="before")
    @classmethod
    def strip_at(cls, value: str) -> str:
        return value.strip().removeprefix("@")

    @field_validator("default_reminder_hours", mode="before")
    @classmethod
    def parse_hours(cls, value: str | list[int]) -> list[int]:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def contact_url(self) -> str:
        value = self.community_contact.strip()
        if value.startswith(("https://", "http://", "tg://")):
            return value
        return f"https://t.me/{value.removeprefix('@')}"

    @property
    def deep_link_base(self) -> str:
        return f"https://t.me/{self.bot_username}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

