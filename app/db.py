"""数据库引擎、会话与建库初始化。"""

from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger("downtime.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """建库建表：创建全部表结构，以及"同设备班次不重叠"的排他约束。

    带重试以等待 Compose 中数据库就绪。
    """
    from . import models  # noqa: F401  确保模型已注册到 metadata

    last_err: Exception | None = None
    for _ in range(retries):
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
            break
        except OperationalError as exc:  # 数据库尚未就绪
            last_err = exc
            time.sleep(delay)
    else:
        raise RuntimeError(f"cannot initialize database: {last_err}")
    _create_exclusion_constraint()


def _create_exclusion_constraint() -> None:
    """为 shifts 增加排他约束（半开区间 [start, end)，相邻不算重叠）。

    依赖 btree_gist 扩展（官方 postgres 镜像自带）；个别精简发行版缺失时
    降级为仅应用层校验并输出告警。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        ALTER TABLE shifts
                        ADD CONSTRAINT shifts_no_overlap_excl
                        EXCLUDE USING gist (
                            device_id WITH =,
                            tstzrange(start_ts, end_ts, '[)') WITH &&
                        );
                    EXCEPTION WHEN duplicate_object THEN
                        NULL;
                    END
                    $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "btree_gist 不可用，未创建班次排他约束（应用层重叠校验仍生效）：%s",
            exc,
        )
