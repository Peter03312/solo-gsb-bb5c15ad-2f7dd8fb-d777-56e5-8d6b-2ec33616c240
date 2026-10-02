"""HTTP API 冒烟测试（SQLite，绕过 PG 建库）。

运行：python -m unittest tests.test_api_sqlite
"""

import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import DateTime as SA_DateTime

from app import db
from app.main import app


def dt(h: int, m: int = 0):
    return datetime(2026, 10, 2, h, m, tzinfo=timezone(timedelta(hours=8))).isoformat()


class _KeepTZDateTime(SA_DateTime):
    def bind_processor(self, dialect):
        def process(value):
            return None if value is None else value.isoformat()
        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            return None if value is None else datetime.fromisoformat(value)
        return process


class APITest(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

        sqlite_dialect.colspecs[SA_DateTime] = _KeepTZDateTime
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        db._engine = engine
        for table in db.metadata_obj.sorted_tables:
            for column in table.columns:
                column.server_default = None
        db.metadata_obj.create_all(engine)

        # 跳过 lifespan 中的 PG init_db（含 CREATE EXTENSION）
        self.client = TestClient(app)

    def test_api_flows(self):
        c = self.client

        # 未知设备
        r = c.post("/api/devices/NOPE/shifts",
                   json={"start": dt(8), "end": dt(12)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "NOT_FOUND")

        # 登记设备
        r = c.post("/api/devices", json={"code": "D1", "name": "泵A"})
        self.assertEqual(r.status_code, 201, r.text)
        # 重复登记 → 幂等 201 + replayed
        r = c.post("/api/devices", json={"code": "D1", "name": "泵A"})
        self.assertEqual(r.json()["status"], "replayed")

        # 非法区间
        r = c.post("/api/devices/D1/shifts",
                   json={"start": dt(13), "end": dt(12)})
        self.assertEqual(r.status_code, 422)

        # 不带时区
        r = c.post("/api/devices/D1/shifts",
                   json={"start": "2026-10-02T08:00:00",
                         "end": "2026-10-02T12:00:00"})
        self.assertEqual(r.status_code, 422)

        # 非整分钟
        r = c.post("/api/devices/D1/shifts",
                   json={"start": "2026-10-02T08:00:30+08:00",
                         "end": "2026-10-02T12:00:00+08:00"})
        self.assertEqual(r.status_code, 422)

        # 正常班次
        r = c.post("/api/devices/D1/shifts",
                   json={"start": dt(8), "end": dt(12)})
        self.assertEqual(r.status_code, 201, r.text)
        shift_id = r.json()["id"]

        # 未结算查询
        r = c.get(f"/api/devices/D1/shifts/{shift_id}/result")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "SHIFT_NOT_SETTLED")

        # 事件：故障 + 待判重叠，故障优先
        c.post("/api/devices/D1/events",
               json={"source": "mes", "version": 1,
                     "start": dt(9), "end": dt(11), "reason": "待判"})
        r = c.post("/api/devices/D1/events",
                   json={"source": "scada", "version": 1,
                         "start": dt(9, 30), "end": dt(10, 30), "reason": "故障"})
        self.assertEqual(r.status_code, 200, r.text)

        # 结算
        r = c.post(f"/api/devices/D1/shifts/{shift_id}/settle")
        self.assertEqual(r.status_code, 200, r.text)
        rev1 = r.json()
        self.assertEqual(rev1["revision_no"], 1)
        self.assertEqual(rev1["minutes"]["fault"], 60)
        self.assertEqual(rev1["minutes"]["pending"], 60)
        self.assertEqual(rev1["minutes"]["normal"], 120)

        # 迟到更正：保养 v2 覆盖 10:00-10:30
        r = c.post("/api/devices/D1/events",
                   json={"source": "mes", "version": 2,
                         "start": dt(10), "end": dt(10, 30), "reason": "保养"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["new_revisions"]), 1)
        rev2 = body["new_revisions"][0]
        self.assertEqual(rev2["revision_no"], 2)
        self.assertEqual(rev2["minutes"]["maintenance"], 30)
        self.assertEqual(rev2["minutes"]["fault"], 30)
        self.assertEqual(rev2["minutes"]["pending"], 60)
        self.assertEqual(rev2["delta"]["maintenance"], 30)
        self.assertEqual(rev2["delta"]["fault"], -30)

        # 幂等重放
        r = c.post("/api/devices/D1/events",
                   json={"source": "mes", "version": 2,
                         "start": dt(10), "end": dt(10, 30), "reason": "保养"})
        self.assertEqual(r.json()["status"], "replayed")

        # 同版本异内容
        r = c.post("/api/devices/D1/events",
                   json={"source": "mes", "version": 2,
                         "start": dt(10), "end": dt(10, 45), "reason": "保养"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "VERSION_CONTENT_MISMATCH")

        # 跳号
        r = c.post("/api/devices/D1/events",
                   json={"source": "mes", "version": 9,
                         "start": dt(11), "end": dt(11, 30), "reason": "保养"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "VERSION_GAP")

        # 查询最新 / 指定修订 / 未知修订
        r = c.get(f"/api/devices/D1/shifts/{shift_id}/result")
        self.assertEqual(r.json()["revision_no"], 2)
        r = c.get(f"/api/devices/D1/shifts/{shift_id}/result?revision=1")
        body = r.json()
        self.assertEqual(body["revision_no"], 1)
        self.assertEqual(body["minutes"]["fault"], 60)  # 历史保留
        r = c.get(f"/api/devices/D1/shifts/{shift_id}/result?revision=99")
        self.assertEqual(r.status_code, 404)

        # 修订清单
        r = c.get(f"/api/devices/D1/shifts/{shift_id}/revisions")
        self.assertEqual([x["revision_no"] for x in r.json()["revisions"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
