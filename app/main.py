from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import services
from .attribution import AttributionError
from .config import get_settings
from .db import get_engine, init_db
from .errors import APIError
from .schemas import DeviceIn, EventIn, ShiftIn

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 等待 PostgreSQL 就绪后建库建表（Compose 中应用可能早于数据库启动）
    last_exc = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            init_db()
            break
        except OperationalError as exc:
            last_exc = exc
            time.sleep(settings.db_connect_retry_interval)
    else:
        raise RuntimeError(f"数据库在 {settings.db_connect_retries} 次重试后仍不可用") from last_exc
    yield


app = FastAPI(
    title="设备停机归因服务",
    version="1.0.0",
    description="按班次对半开区间停机事件做优先级归因，支持幂等摄入、版本链校验、"
                "迟到更正重算与修订差额查询。",
    lifespan=lifespan,
)


def get_db():
    engine = get_engine()
    with Session(engine) as session:
        yield session


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": str(exc.message),
                 "details": exc.details},
    )


@app.exception_handler(AttributionError)
async def attribution_error_handler(request: Request, exc: AttributionError):
    return JSONResponse(
        status_code=400,
        content={"error": "ATTRIBUTION_ERROR", "message": str(exc),
                 "details": {}},
    )


@app.get("/healthz", tags=["meta"])
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------

@app.post("/api/devices", status_code=201, tags=["devices"])
def create_device(payload: DeviceIn, db: Session = Depends(get_db)):
    """登记设备（编号已存在且名称一致时幂等返回）。"""
    with db.begin():
        device, replayed = services.create_device(db, payload)
    return {"status": "replayed" if replayed else "created", "device": device}


# ---------------------------------------------------------------------------
# 班次
# ---------------------------------------------------------------------------

@app.post("/api/devices/{device_code}/shifts", status_code=201, tags=["shifts"])
def create_shift(device_code: str, payload: ShiftIn, db: Session = Depends(get_db)):
    """为设备登记班次。同设备班次时间区间不可重叠。"""
    with db.begin():
        shift = services.create_shift(db, device_code, payload)
    return shift


@app.get("/api/devices/{device_code}/shifts", tags=["shifts"])
def list_shifts(device_code: str, db: Session = Depends(get_db)):
    with db.begin():
        return {"shifts": services.list_shifts(db, device_code)}


@app.post("/api/devices/{device_code}/shifts/{shift_id}/settle",
          tags=["shifts"])
def settle_shift(device_code: str, shift_id: int, db: Session = Depends(get_db)):
    """结算班次：固化归因结果（修订号 1）。重复结算返回最新修订。"""
    with db.begin():
        return services.settle_shift(db, device_code, shift_id)


@app.get("/api/devices/{device_code}/shifts/{shift_id}/result", tags=["shifts"])
def get_shift_result(device_code: str, shift_id: int,
                     revision: int | None = None, db: Session = Depends(get_db)):
    """查询归因结果；?revision=N 指定历史修订，缺省返回最新修订。"""
    with db.begin():
        return services.get_revision(db, device_code, shift_id, revision)


@app.get("/api/devices/{device_code}/shifts/{shift_id}/revisions", tags=["shifts"])
def list_shift_revisions(device_code: str, shift_id: int,
                         db: Session = Depends(get_db)):
    """列出班次的全部修订（首次结算 + 各次迟到更正）。"""
    with db.begin():
        return services.list_revisions(db, device_code, shift_id)


# ---------------------------------------------------------------------------
# 停机事件
# ---------------------------------------------------------------------------

@app.post("/api/devices/{device_code}/events", tags=["events"])
def ingest_event(device_code: str, payload: EventIn, db: Session = Depends(get_db)):
    """摄入停机事件。

    * 同 (source, version) 同内容 → 幂等返回（status=replayed）；
    * 同版本异内容 → 409 VERSION_CONTENT_MISMATCH；
    * 版本跳号 → 409 VERSION_GAP；
    * 迟到更正会按事件时间重算所有重叠的已结算班次，返回新修订与差额。
    """
    with db.begin():
        return services.ingest_event(db, device_code, payload)


@app.get("/api/devices/{device_code}/events", tags=["events"])
def list_device_events(device_code: str, db: Session = Depends(get_db)):
    with db.begin():
        return {"events": services.list_device_events(db, device_code)}
