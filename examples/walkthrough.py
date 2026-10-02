#!/usr/bin/env python3
"""端到端调用示例（无需第三方依赖）。

用法：
    python examples/walkthrough.py                 # 默认 http://127.0.0.1:8080
    BASE_URL=http://127.0.0.1:18080 python examples/walkthrough.py

演示顺序：
    1. 登记设备、建立 08:00-12:00 班次；
    2. 上报 v1 待判事件并结算，得到修订号 1；
    3. 同内容重放（幂等）、同版本异内容/跳号被拒绝；
    4. 迟到的保养更正 v2 触发重算，生成修订号 2 与差额；
    5. 分别查询最新修订与历史修订 1。
"""

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
DEVICE = "CN-01-LINE-A"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
            print(f"{method} {path} -> {resp.status}")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        print(f"{method} {path} -> {exc.code} {payload['error']}: {payload['message']}")
        return exc.code, payload


def show(title, payload):
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main():
    print(f"目标服务: {BASE}")

    call("POST", "/api/devices", {"code": DEVICE, "name": "一号产线A"})

    _, shift = call("POST", f"/api/devices/{DEVICE}/shifts", {
        "start": "2026-10-02T08:00:00+08:00",
        "end": "2026-10-02T12:00:00+08:00",
    })
    shift_id = shift["id"]

    call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "mes-cn01", "version": 1,
        "start": "2026-10-02T09:00:00+08:00",
        "end": "2026-10-02T11:00:00+08:00",
        "reason": "待判",
    })
    call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "scada", "version": 1,
        "start": "2026-10-02T09:30:00+08:00",
        "end": "2026-10-02T10:30:00+08:00",
        "reason": "故障",
    })

    _, rev1 = call("POST", f"/api/devices/{DEVICE}/shifts/{shift_id}/settle")
    show("结算：修订号 1（故障优先于待判）", rev1)

    print("\n----- 幂等与版本链校验 -----")
    code, replayed = call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "mes-cn01", "version": 1,
        "start": "2026-10-02T09:00:00+08:00",
        "end": "2026-10-02T11:00:00+08:00",
        "reason": "待判",
    })
    assert replayed["status"] == "replayed"

    call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "mes-cn01", "version": 1,
        "start": "2026-10-02T09:00:00+08:00",
        "end": "2026-10-02T11:01:00+08:00",
        "reason": "待判",
    })  # 409 VERSION_CONTENT_MISMATCH
    call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "mes-cn01", "version": 5,
        "start": "2026-10-02T11:00:00+08:00",
        "end": "2026-10-02T11:30:00+08:00",
        "reason": "保养",
    })  # 409 VERSION_GAP

    print("\n----- 迟到更正（保养优先于故障）-----")
    _, corrected = call("POST", f"/api/devices/{DEVICE}/events", {
        "source": "mes-cn01", "version": 2,
        "start": "2026-10-02T10:00:00+08:00",
        "end": "2026-10-02T10:30:00+08:00",
        "reason": "保养",
    })
    show("迟到更正响应（内含新修订与差额）", corrected)

    _, latest = call("GET", f"/api/devices/{DEVICE}/shifts/{shift_id}/result")
    show("查询最新修订", latest)

    _, old = call("GET",
                  f"/api/devices/{DEVICE}/shifts/{shift_id}/result?revision=1")
    show("查询历史修订号 1（原结果保留）", old)

    _, revs = call("GET", f"/api/devices/{DEVICE}/shifts/{shift_id}/revisions")
    show("全部修订", revs)


if __name__ == "__main__":
    main()
