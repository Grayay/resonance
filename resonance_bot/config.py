from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    superadmin_id: int
    database_url: str
    channel_id: str
    community_contact: str
    timezone: str = "Europe/Moscow"
    bot_username: str
    waitlist_offer_ttl_minutes: int = Field(default=120, ge=5, le=1440)
    scheduler_poll_seconds: int = Field(default=20, ge=5, le=300)
    default_reminder_hours: list[int] = Field(default_factory=lambda: [24, 3])
    log_level: str = "INFO"

    @field_validator("bot_username", mode="before")
    @classmethod
    def strip_at(cls, value: str) -> str:
        return value.strip().removeprefix("@")

    @field_validator("default_reminder_hours", mode="before")
    @classmethod
    def parse_hours(cls, value: str | list[int]) -> list[int]:
        if isinstance(value, str):
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

