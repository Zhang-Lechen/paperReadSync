from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class State(StrEnum):
    LOCAL_ONLY = "LOCAL_ONLY"
    READY = "READY"
    PROBING = "PROBING"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    SYNCING = "SYNCING"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class Book(Model):
    book_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=500)
    translator: str = Field(default="", max_length=500)
    publisher: str = Field(default="", max_length=500)
    isbn: str = Field(default="", max_length=64)
    corresponding_edition: bool = False
    confirmed_by_user: bool = False
    confirmed_at: str | None = None
    origin: Literal["official", "manual", "mock"] = "manual"
    discovery_source: Literal["catalog", "shelf"] = "catalog"
    reader_link: str = Field(default="", max_length=2048)


class EntryInput(Model):
    entry_id: UUID = Field(default_factory=uuid4)
    account_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    book_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    reading_date: date
    timezone: str = "Asia/Shanghai"
    duration_seconds: StrictInt = Field(gt=0, le=86400)
    source: Literal["paper"] = "paper"
    session_start: datetime | None = None
    session_end: datetime | None = None
    note: str = Field(default="", max_length=4000)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("未知时区") from None
        return value

    @model_validator(mode="after")
    def validate_date_and_session(self):
        zone = ZoneInfo(self.timezone)
        if self.reading_date > datetime.now(zone).date():
            raise ValueError("阅读日期不能在未来")
        if (self.session_start is None) != (self.session_end is None):
            raise ValueError("会话开始和结束时间需同时提供")
        if self.session_start is not None:
            start, end = self.session_start, self.session_end
            if start.utcoffset() is None or end.utcoffset() is None:
                raise ValueError("会话时间需要时区偏移")
            if end <= start or end > datetime.now(timezone.utc):
                raise ValueError("会话结束时间无效")
            if start.astimezone(zone).date() != self.reading_date:
                raise ValueError("会话开始日期与阅读日期不一致")
            if (end - start).total_seconds() < self.duration_seconds:
                raise ValueError("阅读时长不能超过会话时间范围")
        return self


class StatsPair(Model):
    book_id: str
    account_id: str
    book_seconds: StrictInt = Field(ge=0)
    period_seconds: StrictInt = Field(ge=0)
    period: Literal["monthly"] = "monthly"
    period_key: str
    captured_at: str = Field(default_factory=utc_now)
    source: Literal["official", "mock"]
    identity_hash: str
    server_timestamp: int | None = None
    # Whitelisted response evidence only, no original authentication containers.
    evidence: dict = Field(default_factory=dict, repr=False)
