"""服务层集成测试（SQLite 替身，不验证 PG 专属的 GiST 排他约束）。

运行：python -m unittest tests.test_services_sqlite
"""

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.types import DateTime as SA_DateTime

from app import db
from app import services
from app.errors import APIError, NotFound, UnprocessableShift
from app.schemas import DeviceIn, EventIn, ShiftIn

CST = timezone(timedelta(hours=8))


def dt(h: int, m: int = 0, day: int = 2):
    return datetime(2026, 10, day, h, m, tzinfo=CST)


class _KeepTZDateTime(SA_DateTime):
    """SQLite 上以 ISO8601 原文存取，保留 UTC 偏移，模拟 timestamptz。"""

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            if value.tzinfo is None:
                raise ValueError("仅接受带时区时间")
            return value.isoformat()
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            return datetime.fromisoformat(value)
        return process


class ServiceTest(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

        sqlite_dialect.colspecs[SA_DateTime] = _KeepTZDateTime
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        db._engine = engine
        # PG 的 now() 服务器默认值在 SQLite 上剥离（测试不依赖它们）
        for table in db.metadata_obj.sorted_tables:
            for column in table.columns:
                column.server_default = None
        # 不执行 btree_gist 与排他约束（PG 专属）
        db.metadata_obj.create_all(engine)
        self.engine = engine

    def session(self):
        return Session(self.engine)

    def test_full_lifecycle(self):
        with self.session() as s, s.begin():
            dev, _ = services.create_device(s, DeviceIn(code="D1", name="泵A"))
        self.assertEqual(dev["code"], "D1")

        # 重复登记同名设备 → 幂等
        with self.session() as s, s.begin():
            dev2, replayed = services.create_device(s, DeviceIn(code="D1", name="泵A"))
        self.assertTrue(replayed)

        # 未知设备报错
        with self.session() as s:
            with self.assertRaises(NotFound):
                services.create_shift(s, "NOPE", ShiftIn(start=dt(8), end=dt(12)))

        # 建班次 08:00-12:00
        with self.session() as s, s.begin():
            shift = services.create_shift(s, "D1", ShiftIn(start=dt(8), end=dt(12)))
        shift_id = shift["id"]

        # 非法区间
        with self.session() as s:
            with self.assertRaises(Exception):
                services.create_shift(s, "D1", ShiftIn(start=dt(13), end=dt(13)))

        # v1: 故障 09:00-10:00
        with self.session() as s, s.begin():
            r1 = services.ingest_event(s, "D1", EventIn(
                source="mes", version=1, start=dt(9), end=dt(10), reason="故障"))
        self.assertEqual(r1["status"], "accepted")
        self.assertEqual(r1["new_revisions"], [])

        # 同 source/version 同内容 → 幂等
        with self.session() as s, s.begin():
            r1b = services.ingest_event(s, "D1", EventIn(
                source="mes", version=1, start=dt(9), end=dt(10), reason="故障"))
        self.assertEqual(r1b["status"], "replayed")
        self.assertEqual(r1b["new_revisions"], [])

        # 同版本异内容 → 拒绝
        with self.session() as s:
            with self.assertRaises(APIError) as cm:
                with s.begin():
                    services.ingest_event(s, "D1", EventIn(
                        source="mes", version=1, start=dt(9), end=dt(10, 30),
                        reason="故障"))
        self.assertEqual(cm.exception.code, "VERSION_CONTENT_MISMATCH")

        # 跳号 v3 → 拒绝
        with self.session() as s:
            with self.assertRaises(APIError) as cm:
                with s.begin():
                    services.ingest_event(s, "D1", EventIn(
                        source="mes", version=3, start=dt(10), end=dt(11),
                        reason="待判"))
        self.assertEqual(cm.exception.code, "VERSION_GAP")

        # 首版非 1 → 拒绝
        with self.session() as s:
            with self.assertRaises(APIError) as cm:
                with s.begin():
                    services.ingest_event(s, "D1", EventIn(
                        source="scada", version=2, start=dt(10), end=dt(11),
                        reason="待判"))
        self.assertEqual(cm.exception.code, "VERSION_GAP")

        # 结算
        with self.session() as s, s.begin():
            rev1 = services.settle_shift(s, "D1", shift_id)
        self.assertEqual(rev1["revision_no"], 1)
        self.assertEqual(rev1["kind"], "settlement")
        self.assertEqual(rev1["minutes"]["fault"], 60)
        self.assertEqual(rev1["minutes"]["normal"], 180)
        self.assertEqual(rev1["delta"], None)
        self.assertEqual(rev1["basis"][0]["source"], "mes")
        self.assertEqual(rev1["basis"][0]["version"], 1)

        # 重复结算 → 仍返回 rev1（结果保留）
        with self.session() as s, s.begin():
            rev_again = services.settle_shift(s, "D1", shift_id)
        self.assertEqual(rev_again["revision_no"], 1)

        # v2: 保养 09:30-10:00（迟到更正，与故障重叠 → 保养优先）
        with self.session() as s, s.begin():
            r2 = services.ingest_event(s, "D1", EventIn(
                source="mes", version=2, start=dt(9, 30), end=dt(10),
                reason="保养"))
        self.assertEqual(r2["status"], "accepted")
        self.assertEqual(len(r2["new_revisions"]), 1)
        rev2 = r2["new_revisions"][0]
        self.assertEqual(rev2["revision_no"], 2)
        self.assertEqual(rev2["kind"], "correction")
        self.assertEqual(rev2["trigger_event"], {"source": "mes", "version": 2})
        self.assertEqual(rev2["minutes"]["maintenance"], 30)
        self.assertEqual(rev2["minutes"]["fault"], 30)
        # 差额
        self.assertEqual(rev2["delta"]["maintenance"], 30)
        self.assertEqual(rev2["delta"]["fault"], -30)
        self.assertEqual(rev2["delta"]["normal"], 0)

        # 无变化的更正（v3 完全被保养覆盖的低优先级待判）→ 不产生新修订
        with self.session() as s, s.begin():
            r3 = services.ingest_event(s, "D1", EventIn(
                source="mes", version=3, start=dt(9, 40), end=dt(9, 50),
                reason="待判"))
        self.assertEqual(r3["new_revisions"], [])

        # 查询：最新、指定修订、未知修订
        with self.session() as s, s.begin():
            latest = services.get_revision(s, "D1", shift_id, None)
            old = services.get_revision(s, "D1", shift_id, 1)
            with self.assertRaises(NotFound):
                services.get_revision(s, "D1", shift_id, 9)
        self.assertEqual(latest["revision_no"], 2)
        self.assertEqual(old["minutes"]["fault"], 60)  # 历史结果保持原样

        # 修订清单
        with self.session() as s, s.begin():
            listing = services.list_revisions(s, "D1", shift_id)
        self.assertEqual([r["revision_no"] for r in listing["revisions"]], [1, 2])

    def test_unsettled_shift_result(self):
        with self.session() as s, s.begin():
            services.create_device(s, DeviceIn(code="D2"))
            shift = services.create_shift(s, "D2", ShiftIn(start=dt(8), end=dt(12)))
        with self.session() as s:
            with self.assertRaises(UnprocessableShift):
                services.get_revision(s, "D2", shift["id"], None)

    def test_correction_touches_all_overlapping_settled_shifts(self):
        # 两个相邻（不重叠）班次；迟到事件横跨边界，两个都应重算
        with self.session() as s, s.begin():
            services.create_device(s, DeviceIn(code="D3"))
            a = services.create_shift(s, "D3", ShiftIn(start=dt(8), end=dt(10)))
            b = services.create_shift(s, "D3", ShiftIn(start=dt(10), end=dt(12)))
            services.settle_shift(s, "D3", a["id"])
            services.settle_shift(s, "D3", b["id"])

        with self.session() as s, s.begin():
            r = services.ingest_event(s, "D3", EventIn(
                source="x", version=1, start=dt(9, 30), end=dt(10, 30),
                reason="故障"))
        self.assertEqual(len(r["new_revisions"]), 2)


if __name__ == "__main__":
    unittest.main()
