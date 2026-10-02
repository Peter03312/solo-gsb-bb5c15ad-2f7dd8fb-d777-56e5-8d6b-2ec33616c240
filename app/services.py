"""业务服务层：设备/班次/事件写入与结算、修订重算。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import attribution as attr
from .db import (
    devices,
    downtime_events,
    event_sources,
    shift_revisions,
    shifts,
)
from .errors import APIError, Conflict, NotFound, UnprocessableShift
from .schemas import DeviceIn, EventIn, ShiftIn


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _iso(value: datetime) -> str:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    """库内读出的时间统一补为 UTC 感知时间（PostgreSQL timestamptz 本就带时区；
    此兜底主要服务于本地 SQLite 冒烟）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _device_payload(row) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name,
            "created_at": _iso(row.created_at)}


def _shift_payload(row) -> dict:
    return {"id": row.id, "device_id": row.device_id,
            "start": _iso(row.start_at), "end": _iso(row.end_at),
            "created_at": _iso(row.created_at)}


def _event_payload(row) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "version": row.version,
        "device_id": row.device_id,
        "start": _iso(row.start_at),
        "end": _iso(row.end_at),
        "reason": row.reason,
        "received_at": _iso(row.received_at),
    }


# ---------------------------------------------------------------------------
# 设备 / 班次
# ---------------------------------------------------------------------------

def get_device(conn, code: str, *, required: bool = True):
    row = conn.execute(select(devices).where(devices.c.code == code)).first()
    if row is None and required:
        raise NotFound(f"未知设备: {code!r}", details={"device_code": code})
    return row


def create_device(conn, payload: DeviceIn) -> tuple[dict, bool]:
    existing = get_device(conn, payload.code, required=False)
    if existing is not None:
        if payload.name and payload.name != existing.name:
            raise Conflict(
                f"设备编号 {payload.code!r} 已存在且名称不同",
                code="DEVICE_CODE_EXISTS",
                details={"device_code": payload.code, "existing_name": existing.name},
            )
        return _device_payload(existing), True  # 幂等返回

    res = conn.execute(devices.insert().values(code=payload.code, name=payload.name)
                       .returning(devices))
    return _device_payload(res.first()), False


def create_shift(conn, device_code: str, payload: ShiftIn) -> dict:
    device = get_device(conn, device_code)
    start = payload.start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end = payload.end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if end <= start:
        raise APIError("非法班次区间：end 必须严格晚于 start",
                       code="INVALID_RANGE")
    try:
        res = conn.execute(
            shifts.insert().values(device_id=device.id, start_at=start, end_at=end)
            .returning(shifts)
        )
    except IntegrityError as exc:
        if "shifts_device_no_overlap" in str(exc.orig):
            raise Conflict(
                f"设备 {device_code!r} 在 {start.isoformat()} ~ {end.isoformat()} "
                "区间与既有班次重叠（班次按半开区间处理）",
                code="SHIFT_OVERLAP",
            ) from exc
        raise
    return _shift_payload(res.first())


def list_shifts(conn, device_code: str) -> list[dict]:
    device = get_device(conn, device_code)
    rows = conn.execute(
        select(shifts).where(shifts.c.device_id == device.id)
        .order_by(shifts.c.start_at)
    ).all()
    return [_shift_payload(r) for r in rows]


def get_shift_row(conn, device_code: str, shift_id: int):
    device = get_device(conn, device_code)
    row = conn.execute(
        select(shifts).where(shifts.c.id == shift_id,
                             shifts.c.device_id == device.id)
    ).first()
    if row is None:
        raise NotFound(f"设备 {device_code!r} 下不存在班次 {shift_id}",
                       details={"device_code": device_code, "shift_id": shift_id})
    return device, row


# ---------------------------------------------------------------------------
# 归因与修订
# ---------------------------------------------------------------------------

def _content_hash(device_id: int, start: datetime, end: datetime, reason: str) -> str:
    body = json.dumps({
        "device_id": device_id,
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "reason": reason,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _events_for_shift(conn, shift_row) -> list[attr.Event]:
    rows = conn.execute(
        select(downtime_events)
        .where(
            downtime_events.c.device_id == shift_row.device_id,
            downtime_events.c.start_at < shift_row.end_at,
            downtime_events.c.end_at > shift_row.start_at,
        )
        .order_by(downtime_events.c.start_at)
    ).all()
    return [
        attr.Event(
            source=r.source,
            version=r.version,
            start=_as_utc(r.start_at),
            end=_as_utc(r.end_at),
            reason=r.reason,
        )
        for r in rows
    ]


def _revision_payload(rev_row, *, delta: dict | None = None) -> dict:
    minutes = {
        "maintenance": rev_row.minutes_maintenance,
        "fault": rev_row.minutes_fault,
        "pending": rev_row.minutes_pending,
        "normal": rev_row.minutes_normal,
    }
    trigger = None
    if rev_row.trigger_source is not None:
        trigger = {"source": rev_row.trigger_source,
                   "version": rev_row.trigger_version}
    return {
        "revision_id": rev_row.id,
        "revision_no": rev_row.revision_no,
        "kind": rev_row.kind,
        "created_at": _iso(rev_row.created_at),
        "minutes": minutes,
        "total_minutes": rev_row.total_minutes,
        "basis": json.loads(rev_row.basis),
        "trigger_event": trigger,
        "delta": delta,
    }


def _latest_revision(conn, shift_id: int):
    return conn.execute(
        select(shift_revisions)
        .where(shift_revisions.c.shift_id == shift_id)
        .order_by(shift_revisions.c.revision_no.desc())
    ).first()


def _basis_json(result: dict) -> str:
    basis = []
    for b in result["basis"]:
        basis.append({
            "source": b["source"],
            "version": b["version"],
            "reason": b["reason"],
            "minutes": b["minutes"],
            "segments": [
                {"start": _iso(s["start"]), "end": _iso(s["end"]),
                 "minutes": s["minutes"]}
                for s in b["segments"]
            ],
        })
    return json.dumps(basis, ensure_ascii=False, sort_keys=True)


def _result_signature(result: dict) -> tuple:
    minutes = result["minutes"]
    return (
        minutes["maintenance"],
        minutes["fault"],
        minutes["pending"],
        minutes["normal"],
        tuple((b["source"], b["version"]) for b in result["basis"]),
    )


def _insert_revision(conn, shift_row, result: dict, *, kind: str,
                     trigger: dict | None, delta: dict | None):
    latest = _latest_revision(conn, shift_row.id)
    revision_no = 1 if latest is None else latest.revision_no + 1
    minutes = result["minutes"]
    res = conn.execute(
        shift_revisions.insert().values(
            shift_id=shift_row.id,
            revision_no=revision_no,
            kind=kind,
            minutes_maintenance=minutes["maintenance"],
            minutes_fault=minutes["fault"],
            minutes_pending=minutes["pending"],
            minutes_normal=minutes["normal"],
            total_minutes=result["total_minutes"],
            basis=_basis_json(result),
            trigger_source=trigger["source"] if trigger else None,
            trigger_version=trigger["version"] if trigger else None,
        ).returning(shift_revisions)
    )
    return _revision_payload(res.first(), delta=delta)


def settle_shift(conn, device_code: str, shift_id: int) -> dict:
    _, shift = get_shift_row(conn, device_code, shift_id)

    latest = _latest_revision(conn, shift.id)
    if latest is not None:
        # 已结算：结算结果永久保留，重复结算直接返回当前最新修订
        return _revision_payload(latest)

    result = attr.attribute(_as_utc(shift.start_at), _as_utc(shift.end_at),
                            _events_for_shift(conn, shift))
    return _insert_revision(conn, shift, result, kind="settlement",
                            trigger=None, delta=None)


def _recompute_settled_shift(conn, shift_row, trigger: dict) -> dict | None:
    """迟到更正到达后重算已结算班次；无变化则不产生新修订。"""
    latest = _latest_revision(conn, shift_row.id)
    if latest is None:
        return None  # 尚未结算，等待显式结算即可

    result = attr.attribute(_as_utc(shift_row.start_at),
                            _as_utc(shift_row.end_at),
                            _events_for_shift(conn, shift_row))
    new_sig = _result_signature(result)
    old_sig = (
        latest.minutes_maintenance,
        latest.minutes_fault,
        latest.minutes_pending,
        latest.minutes_normal,
        tuple((b["source"], b["version"]) for b in json.loads(latest.basis)),
    )
    if new_sig == old_sig:
        return None

    delta = {
        "maintenance": result["minutes"]["maintenance"] - latest.minutes_maintenance,
        "fault": result["minutes"]["fault"] - latest.minutes_fault,
        "pending": result["minutes"]["pending"] - latest.minutes_pending,
        "normal": result["minutes"]["normal"] - latest.minutes_normal,
    }
    return _insert_revision(conn, shift_row, result, kind="correction",
                            trigger=trigger, delta=delta)


# ---------------------------------------------------------------------------
# 事件摄入（版本链 + 幂等 + 迟到更正）
# ---------------------------------------------------------------------------

def ingest_event(conn, device_code: str, payload: EventIn) -> dict:
    device = get_device(conn, device_code)
    start = payload.start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    end = payload.end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if end <= start:
        raise APIError("非法事件区间：end 必须严格晚于 start",
                       code="INVALID_RANGE")

    content_hash = _content_hash(device.id, start, end, payload.reason)
    source = payload.source
    version = payload.version

    # 锁住来源行，串行化同来源的版本号检查
    src = conn.execute(
        select(event_sources).where(event_sources.c.source == source)
        .with_for_update()
    ).first()

    replayed = False
    if src is None:
        if version != 1:
            raise Conflict(
                f"来源 {source!r} 的首个事件版本必须为 1，收到 {version}",
                code="VERSION_GAP",
                details={"source": source, "expected": 1, "received": version},
            )
        try:
            conn.execute(event_sources.insert().values(source=source, last_version=1))
        except IntegrityError as exc:
            # 并发首报，让调用方重试即可走幂等/校验分支
            raise Conflict(f"来源 {source!r} 正在被并发写入，请重试",
                           code="SOURCE_CONFLICT") from exc
    else:
        expected = src.last_version + 1
        if version <= src.last_version:
            stored = conn.execute(
                select(downtime_events).where(
                    downtime_events.c.source == source,
                    downtime_events.c.version == version,
                )
            ).first()
            if stored is not None and stored.content_hash == content_hash:
                replayed = True
            else:
                raise Conflict(
                    f"来源 {source!r} 版本 {version} 已存在但内容不同；"
                    "事件不可变更，请用递增版本提交更正",
                    code="VERSION_CONTENT_MISMATCH",
                    details={"source": source, "version": version},
                )
        elif version > expected:
            raise Conflict(
                f"来源 {source!r} 版本跳号：期望 {expected}，收到 {version}",
                code="VERSION_GAP",
                details={"source": source, "expected": expected,
                         "received": version},
            )

    if not replayed:
        try:
            ins = conn.execute(
                downtime_events.insert().values(
                    source=source,
                    version=version,
                    device_id=device.id,
                    start_at=start,
                    end_at=end,
                    reason=payload.reason,
                    content_hash=content_hash,
                ).returning(downtime_events)
            )
        except IntegrityError as exc:
            if "uq_event_source_version" in str(exc.orig):
                # 并发下版本行已落库，交由调用侧重试走幂等判断
                raise Conflict(
                    f"来源 {source!r} 版本 {version} 并发冲突，请重试",
                    code="EVENT_CONFLICT",
                ) from exc
            raise
        event_row = ins.first()
        if src is not None:
            conn.execute(
                event_sources.update()
                .where(event_sources.c.source == source)
                .values(last_version=version)
            )
    else:
        event_row = conn.execute(
            select(downtime_events).where(
                downtime_events.c.source == source,
                downtime_events.c.version == version,
            )
        ).first()

    # 迟到更正：按事件时间重算所有重叠的「已结算」班次
    new_revisions: list[dict] = []
    if not replayed:
        affected = conn.execute(
            select(shifts)
            .join(shift_revisions, shift_revisions.c.shift_id == shifts.c.id)
            .where(
                shifts.c.device_id == device.id,
                shifts.c.start_at < end,
                shifts.c.end_at > start,
            )
            .distinct()
        ).all()
        for shift_row in affected:
            rev = _recompute_settled_shift(
                conn, shift_row, {"source": source, "version": version}
            )
            if rev is not None:
                new_revisions.append(rev)

    return {
        "status": "replayed" if replayed else "accepted",
        "event": _event_payload(event_row),
        "new_revisions": new_revisions,
    }


def list_device_events(conn, device_code: str) -> list[dict]:
    device = get_device(conn, device_code)
    rows = conn.execute(
        select(downtime_events)
        .where(downtime_events.c.device_id == device.id)
        .order_by(downtime_events.c.start_at, downtime_events.c.source,
                  downtime_events.c.version)
    ).all()
    return [_event_payload(r) for r in rows]


# ---------------------------------------------------------------------------
# 结果查询
# ---------------------------------------------------------------------------

def get_revision(conn, device_code: str, shift_id: int,
                 revision_no: int | None) -> dict:
    _, shift = get_shift_row(conn, device_code, shift_id)
    stmt = select(shift_revisions).where(shift_revisions.c.shift_id == shift.id)
    if revision_no is None:
        stmt = stmt.order_by(shift_revisions.c.revision_no.desc()).limit(1)
        row = conn.execute(stmt).first()
        if row is None:
            raise UnprocessableShift(
                f"班次 {shift_id} 尚未结算，暂无归因结果；请先调用结算接口",
                details={"device_code": device_code, "shift_id": shift_id},
            )
        return _revision_payload(row)

    row = conn.execute(
        stmt.where(shift_revisions.c.revision_no == revision_no)
    ).first()
    if row is None:
        raise NotFound(
            f"班次 {shift_id} 不存在修订号 {revision_no}",
            details={"device_code": device_code, "shift_id": shift_id,
                     "revision_no": revision_no},
        )
    return _revision_payload(row)


def list_revisions(conn, device_code: str, shift_id: int) -> dict:
    _, shift = get_shift_row(conn, device_code, shift_id)
    rows = conn.execute(
        select(shift_revisions)
        .where(shift_revisions.c.shift_id == shift.id)
        .order_by(shift_revisions.c.revision_no)
    ).all()
    return {
        "shift": _shift_payload(shift),
        "revisions": [_revision_payload(r) for r in rows],
    }
