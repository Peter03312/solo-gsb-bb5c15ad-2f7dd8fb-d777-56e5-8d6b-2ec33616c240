"""归因引擎规则测试：python -m unittest -v"""

import unittest
from datetime import datetime, timedelta, timezone

from app import attribution as attr

CST = timezone(timedelta(hours= 8))


def dt(h: int, m: int = 0, tz=CST):
    return datetime(2026, 10, 2, h, m, tzinfo=tz)


def ev(source, version, s, e, reason):
    return attr.Event(source=source, version=version, start=s, end=e, reason=reason)


class TestIntervalValidation(unittest.TestCase):
    def test_naive_rejected(self):
        with self.assertRaises(attr.AttributionError):
            attr.validate_aligned_minute(datetime(2026, 10, 2, 8), "x")

    def test_seconds_rejected(self):
        with self.assertRaises(attr.AttributionError):
            attr.validate_aligned_minute(datetime(2026, 10, 2, 8, 0, 30, tzinfo=CST), "x")

    def test_non_positive_range_rejected(self):
        with self.assertRaises(attr.AttributionError):
            attr.attribute(dt(8), dt(8), [])
        with self.assertRaises(attr.AttributionError):
            attr.attribute(dt(9), dt(8), [])

    def test_reason_aliases(self):
        self.assertEqual(attr.normalize_reason("保养"), "maintenance")
        self.assertEqual(attr.normalize_reason("故障"), "fault")
        self.assertEqual(attr.normalize_reason("待判"), "pending")
        with self.assertRaises(attr.AttributionError):
            attr.normalize_reason("其他")


class TestAttribution(unittest.TestCase):
    def test_empty_shift_all_normal(self):
        r = attr.attribute(dt(8), dt(12), [])
        self.assertEqual(r["minutes"],
                         {"maintenance": 0, "fault": 0, "pending": 0, "normal": 240})
        self.assertEqual(r["total_minutes"], 240)
        self.assertEqual(r["basis"], [])

    def test_single_event(self):
        r = attr.attribute(dt(8), dt(12),
                           [ev("s1", 1, dt(9), dt(10), "fault")])
        self.assertEqual(r["minutes"]["fault"], 60)
        self.assertEqual(r["minutes"]["normal"], 180)
        self.assertEqual(len(r["basis"]), 1)
        self.assertEqual(r["basis"][0]["source"], "s1")
        self.assertEqual(r["basis"][0]["version"], 1)
        self.assertEqual(r["basis"][0]["minutes"], 60)

    def test_half_open_boundaries(self):
        # 事件恰好贴齐班次边界：[9,10) 不计入 [8,9)
        r = attr.attribute(dt(8), dt(9),
                           [ev("s1", 1, dt(9), dt(10), "fault")])
        self.assertEqual(r["minutes"]["normal"], 60)
        # 同分钟相切不重叠
        r = attr.attribute(dt(8), dt(10),
                           [ev("s1", 1, dt(8), dt(9), "fault"),
                            ev("s2", 1, dt(9), dt(10), "maintenance")])
        self.assertEqual(r["minutes"]["fault"], 60)
        self.assertEqual(r["minutes"]["maintenance"], 60)

    def test_priority_maintenance_over_fault_over_pending(self):
        # 08:00-10:00 pending，08:30-09:30 fault，09:00-09:30 maintenance
        events = [
            ev("s1", 1, dt(8), dt(10), "pending"),
            ev("s2", 1, dt(8, 30), dt(9, 30), "fault"),
            ev("s3", 1, dt(9), dt(9, 30), "maintenance"),
        ]
        r = attr.attribute(dt(8), dt(10), events)
        m = r["minutes"]
        # maintenance 09:00-09:30 = 30
        # fault       08:30-09:00 = 30
        # pending     08:00-08:30 + 09:30-10:00 = 60
        self.assertEqual(m["maintenance"], 30)
        self.assertEqual(m["fault"], 30)
        self.assertEqual(m["pending"], 60)
        self.assertEqual(m["normal"], 0)
        self.assertEqual(sum(m.values()), 120)
        # 每条事件只计实际贡献
        contrib = {(b["source"], b["version"]): b["minutes"] for b in r["basis"]}
        self.assertEqual(contrib[("s1", 1)], 60)
        self.assertEqual(contrib[("s2", 1)], 30)
        self.assertEqual(contrib[("s3", 1)], 30)

    def test_each_minute_attributed_once_partial_overlap(self):
        # 事件超出班次两头，只应统计班内部分
        r = attr.attribute(dt(9), dt(10),
                           [ev("s1", 1, dt(8), dt(11), "fault")])
        self.assertEqual(r["minutes"]["fault"], 60)
        self.assertEqual(r["minutes"]["normal"], 0)

    def test_timezone_normalization(self):
        # 同一绝对时刻，不同时区表达，结果一致
        tokyo = timezone(timedelta(hours=9))
        r1 = attr.attribute(dt(8), dt(9),
                            [ev("s1", 1, dt(8), dt(9), "fault")])
        r2 = attr.attribute(
            datetime(2026, 10, 2, 9, tzinfo=tokyo),
            datetime(2026, 10, 2, 10, tzinfo=tokyo),
            [ev("s1", 1,
                datetime(2026, 10, 2, 9, tzinfo=tokyo),
                datetime(2026, 10, 2, 10, tzinfo=tokyo),
                "fault")])
        self.assertEqual(r1["minutes"], r2["minutes"])

    def test_tie_break_is_deterministic(self):
        # 同优先级整段重叠，取 source/version 较小者
        r = attr.attribute(dt(8), dt(9),
                           [ev("zzz", 9, dt(8), dt(9), "fault"),
                            ev("aaa", 2, dt(8), dt(9), "fault")])
        self.assertEqual(r["minutes"]["fault"], 60)
        self.assertEqual([(b["source"], b["version"]) for b in r["basis"]],
                         [("aaa", 2)])


if __name__ == "__main__":
    unittest.main()
