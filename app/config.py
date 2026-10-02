from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 完整 SQLAlchemy 连接串，例如
    # postgresql+psycopg2://downtime:downtime@db:5432/downtime
    database_url: str = "postgresql+psycopg2://downtime:downtime@127.0.0.1:5432/downtime"

    # API 监听
    host: str = "0.0.0.0"
    port: int = 8080

    # 启动时等待数据库就绪的重试参数
    db_connect_retries: int = 30
    db_connect_retry_interval: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
