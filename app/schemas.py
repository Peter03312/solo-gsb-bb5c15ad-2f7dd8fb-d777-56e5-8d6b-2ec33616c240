from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .attribution import REASONS, normalize_reason


def _check_tz_aligned(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须携带时区偏移，例如 2026-10-02T08:00:00+08:00")
    if value.second != 0 or value.microsecond != 0:
        raise ValueError(f"{field} 必须精确到整分钟（秒和微秒为 0）")
    return value


class DeviceIn(BaseModel):
    code: str = Field(min_length=1, max_length=64, examples=["CN-01-LINE-A"])
    name: str = Field(default="", max_length=255)


class ShiftIn(BaseModel):
    start: datetime = Field(description="班次开始，带时区、整分钟（包含）")
    end: datetime = Field(description="班次结束，带时区、整分钟（不包含，半开区间）")

    @field_validator("start", "end")
    @classmethod
    def _tz_aligned(cls, v: datetime, info):
        return _check_tz_aligned(v, info.field_name)

    @model_validator(mode="after")
    def _range(self):
        if self.end <= self.start:
            raise ValueError("非法班次区间：end 必须严格晚于 start（半开区间 [start, end)）")
        return self


class EventIn(BaseModel):
    # 来源编号：同一来源的版本号必须从 1 开始连续递增
    source: str = Field(min_length=1, max_length=128, examples=["mes-cn01"])
    version: int = Field(ge=1, description="连续递增版本，从 1 开始")
    start: datetime
    end: datetime
    # 保养/故障/待判，兼容英文枚举
    reason: str = Field(description="停机原因：保养 / 故障 / 待判"
                                    f"（或 {', '.join(REASONS)}）")

    @field_validator("start", "end")
    @classmethod
    def _tz_aligned(cls, v: datetime, info):
        return _check_tz_aligned(v, info.field_name)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str):
        return normalize_reason(v)

    @model_validator(mode="after")
    def _range(self):
        if self.end <= self.start:
            raise ValueError("非法事件区间：end 必须严格晚于 start（半开区间 [start, end)）")
        return self
