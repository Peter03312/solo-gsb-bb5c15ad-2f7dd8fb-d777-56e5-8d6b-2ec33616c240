"""停机归因核心算法（纯函数，不依赖数据库）。

规则
----
* 班次与事件均为「半开区间」[start, end)，即包含起始分钟、不包含结束分钟。
* 时间必须带时区且精确到整分钟。
* 每一个班次分钟只归因一次；同一分钟被多个事件覆盖时：
      保养(maintenance) > 故障(fault) > 待判(pending)
* 优先级相同时，取 source/version 较小的事件作为依据，保证结果确定。
* 输出同时给出未被任何停机事件覆盖的正常分钟数（normal）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

# 原因编码与展示名 / 中文入参别名
REASON_MAINTENANCE = "maintenance"
REASON_FAULT = "fault"
REASON_PENDING = "pending"

REASONS = (REASON_MAINTENANCE, REASON_FAULT, REASON_PENDING)

# 数值越小优先级越高
REASON_PRIORITY = {
    REASON_MAINTENANCE: 0,
    REASON_FAULT: 1,
    REASON_PENDING: 2,
}

REASON_ALIASES = {
    "maintenance": REASON_MAINTENANCE,
    "保养": REASON_MAINTENANCE,
    "fault": REASON_FAULT,
    "故障": REASON_FAULT,
    "pending": REASON_PENDING,
    "待判": REASON_PENDING,
}

MINUTE = timedelta(minutes=1)


class AttributionError(ValueError):
    """输入不满足归因前置条件（区间非法、时间未对齐等）。"""


def normalize_reason(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key not in REASON_ALIASES:
        raise AttributionError(f"未知停机原因: {raw!r}，允许值: {', '.join(REASONS)}")
    return REASON_ALIASES[key]


def validate_aligned_minute(value: datetime, field: str) -> datetime:
    """校验时间带时区且精确到整分钟，统一返回 UTC 时间。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise AttributionError(f"{field} 必须带时区偏移")
    if value.second != 0 or value.microsecond != 0:
        raise AttributionError(f"{field} 必须精确到整分钟（秒/微秒须为 0）")
    return value.astimezone(timezone.utc)


def validate_half_open(start: datetime, end: datetime) -> None:
    if end <= start:
        raise AttributionError(
            f"非法半开区间: end ({end.isoformat()}) 必须严格大于 start ({start.isoformat()})"
        )


def minute_count(start: datetime, end: datetime) -> int:
    """半开区间 [start, end) 包含的整分钟数。"""
    return int((end - start) / MINUTE)


@dataclass(frozen=True)
class Event:
    source: str
    version: int
    start: datetime  # UTC，整分钟
    end: datetime  # UTC，整分钟，end > start
    reason: str  # REASONS 之一


@dataclass(frozen=True)
class BasisEntry:
    """某条事件在本次归因中实际贡献的分钟，作为结果依据。"""

    source: str
    version: int
    reason: str
    start: datetime
    end: datetime
    minutes: int


def _event_sort_key(ev: Event):
    # 优先级高的先占坑；同优先级用来源与版本保证确定性
    return (REASON_PRIORITY[ev.reason], ev.source, ev.version, ev.start)


def attribute(
    shift_start: datetime,
    shift_end: datetime,
    events: Sequence[Event],
) -> dict:
    """计算一个班次的归因结果。

    返回::

        {
          "minutes": {"maintenance": x, "fault": y, "pending": z, "normal": n},
          "total_minutes": t,
          "basis": [BasisEntry, ...],
        }
    """
    shift_start = validate_aligned_minute(shift_start, "shift_start")
    shift_end = validate_aligned_minute(shift_end, "shift_end")
    validate_half_open(shift_start, shift_end)

    total = minute_count(shift_start, shift_end)
    # owner[m] 记录第 m 个班次分钟当前被哪条事件占用
    owner: list[Event | None] = [None] * total
    minutes = {REASON_MAINTENANCE: 0, REASON_FAULT: 0, REASON_PENDING: 0}

    # 高优先级事件先处理，先到先占；重叠的低优先级事件被自然挤出
    for ev in sorted(events, key=_event_sort_key):
        start = validate_aligned_minute(ev.start, f"event {ev.source}#{ev.version} start")
        end = validate_aligned_minute(ev.end, f"event {ev.source}#{ev.version} end")
        validate_half_open(start, end)
        if ev.reason not in REASON_PRIORITY:
            raise AttributionError(f"未知停机原因: {ev.reason!r}")

        lo = max(0, minute_count(shift_start, start))
        hi = min(total, minute_count(shift_start, end))
        for m in range(lo, hi):
            if owner[m] is None:
                owner[m] = ev
                minutes[ev.reason] += 1

    normal = total - sum(minutes.values())

    # 汇总每条事件实际贡献的连续/非连续分钟，按贡献区间拆分给出起止
    basis = _build_basis(shift_start, owner)

    return {
        "minutes": {
            REASON_MAINTENANCE: minutes[REASON_MAINTENANCE],
            REASON_FAULT: minutes[REASON_FAULT],
            REASON_PENDING: minutes[REASON_PENDING],
            "normal": normal,
        },
        "total_minutes": total,
        "basis": basis,
    }


def _build_basis(shift_start: datetime, owner: Sequence[Event | None]) -> list[BasisEntry]:
    """把「每分钟归属」展开为依据列表，同一事件相邻分钟合并为连续区间。"""
    entries: list[BasisEntry] = []

    run_event: Event | None = None
    run_start_idx = 0

    def flush(end_idx: int) -> None:
        if run_event is None:
            return
        entries.append(
            BasisEntry(
                source=run_event.source,
                version=run_event.version,
                reason=run_event.reason,
                start=shift_start + run_start_idx * MINUTE,
                end=shift_start + end_idx * MINUTE,
                minutes=end_idx - run_start_idx,
            )
        )

    for i, ev in enumerate(owner):
        if ev is run_event and ev is not None:
            continue
        flush(i)
        run_event = ev
        run_start_idx = i

    flush(len(owner))

    # 同一事件可能在不相邻的两段各贡献一次，按 source/version 再做一次区间合并汇总
    merged: dict[tuple[str, int], dict] = {}
    order: list[tuple[str, int]] = []
    for e in entries:
        key = (e.source, e.version)
        if key not in merged:
            merged[key] = {
                "source": e.source,
                "version": e.version,
                "reason": e.reason,
                "minutes": 0,
                "segments": [],
            }
            order.append(key)
        merged[key]["minutes"] += e.minutes
        merged[key]["segments"].append({"start": e.start, "end": e.end, "minutes": e.minutes})

    return [merged[k] for k in order]


def basis_events_signature(attribution_result: dict) -> list[tuple[str, int]]:
    """结果中真正贡献过分钟的事件 (source, version)，用于判断修订是否产生变化。"""
    return [(b["source"], b["version"]) for b in attribution_result["basis"]]
