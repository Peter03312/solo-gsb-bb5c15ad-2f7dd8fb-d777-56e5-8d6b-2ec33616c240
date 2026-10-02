"""数据库结构与会话。

关键约束
--------
* ``devices.code`` 唯一；
* 同设备班次使用 GiST 排他约束禁止时间区间重叠（半开区间 [start, end)）；
* 停机事件按 (source, version) 唯一，version 必须从 1 起连续递增；
* 事件内容哈希用于「同来源同版本同内容幂等、同版本异内容拒绝」；
* 班次每次结算/重算在 ``shift_revisions`` 留痕，旧修订永不覆盖。
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.engine import Engine, create_engine

from .config import get_settings

metadata_obj = MetaData()

devices = Table(
    "devices",
    metadata_obj,
    Column("id", Integer, primary_key=True),
    Column("code", String(64), nullable=False, unique=True),
    Column("name", String(255), nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False,
           default=func.now(), server_default=func.current_timestamp()),
)

shifts = Table(
    "shifts",
    metadata_obj,
    Column("id", Integer, primary_key=True),
    Column("device_id", Integer, ForeignKey("devices.id"), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False,
           default=func.now(), server_default=func.current_timestamp()),
    CheckConstraint("end_at > start_at", name="shifts_range_valid"),
    # 排他约束在 init_db() 中通过原生 DDL 追加（GiST + tstzrange，半开区间）
)

# 同设备班次不重叠：btree_gist 让 device_id(=) 可与时间范围(&&)组合进排他约束
SHIFT_EXCLUDE_DDL = """
ALTER TABLE shifts
    ADD CONSTRAINT shifts_device_no_overlap
    EXCLUDE USING gist (
        device_id WITH =,
        tstzrange(start_at, end_at, '[)') WITH &&
    )
"""

event_sources = Table(
    "event_sources",
    metadata_obj,
    Column("source", String(128), primary_key=True),
    # 已接受的最大版本号；下一次事件必须是 last_version + 1
    Column("last_version", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False,
           default=func.now(), server_default=func.current_timestamp()),
)

downtime_events = Table(
    "downtime_events",
    metadata_obj,
    Column("id", Integer, primary_key=True),
    Column("source", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("device_id", Integer, ForeignKey("devices.id"), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("reason", String(16), nullable=False),
    # 对 (device_id, start, end, reason) 规范化后计算的哈希，用于幂等/冲突判断
    Column("content_hash", String(64), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False,
           default=func.now(), server_default=func.current_timestamp()),
    UniqueConstraint("source", "version", name="uq_event_source_version"),
    CheckConstraint("end_at > start_at", name="events_range_valid"),
    CheckConstraint("reason IN ('maintenance', 'fault', 'pending')",
                    name="events_reason_valid"),
)

Index("ix_events_device_time", downtime_events.c.device_id,
      downtime_events.c.start_at, downtime_events.c.end_at)

shift_revisions = Table(
    "shift_revisions",
    metadata_obj,
    Column("id", Integer, primary_key=True),
    Column("shift_id", Integer, ForeignKey("shifts.id"), nullable=False),
    Column("revision_no", Integer, nullable=False),
    # 1 = 首次结算；其后的迟到更正依次递增
    Column("kind", String(16), nullable=False),  # settlement | correction
    Column("minutes_maintenance", Integer, nullable=False),
    Column("minutes_fault", Integer, nullable=False),
    Column("minutes_pending", Integer, nullable=False),
    Column("minutes_normal", Integer, nullable=False),
    Column("total_minutes", Integer, nullable=False),
    # 归因依据：[{source, version, reason, minutes, segments:[...]}]
    Column("basis", Text, nullable=False),
    # 触发本次修订的事件（首次结算为 NULL）
    Column("trigger_source", String(128), nullable=True),
    Column("trigger_version", Integer, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False,
           default=func.now(), server_default=func.current_timestamp()),
    UniqueConstraint("shift_id", "revision_no", name="uq_shift_revision_no"),
)

Index("ix_revisions_shift", shift_revisions.c.shift_id,
      shift_revisions.c.revision_no)


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if settings.database_url.startswith(("postgresql", "postgres")):
            connect_args["connect_timeout"] = 5
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
        )
    return _engine


def init_db() -> None:
    """创建扩展与全部表/约束（幂等）。

    SQLite 方言仅用于本地无数据库时的冒烟测试，会跳过 PG 专属 DDL。
    """
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        metadata_obj.create_all(engine)
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    metadata_obj.create_all(engine)
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'shifts_device_no_overlap'"
        )).scalar()
        if not exists:
            conn.execute(text(SHIFT_EXCLUDE_DDL))
