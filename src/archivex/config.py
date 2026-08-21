from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded once when the application process starts."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twscrape_session_path: Path = Path("/data/twscrape")
    twscrape_wait_timeout_seconds: float = Field(default=0.5, ge=0, le=60)
    twscrape_wait_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    archive_db_path: Path = Path("/data/archive.sqlite3")
    archive_data_dir: Path = Path("/data/archive")
    archive_initial_post_limit: int = Field(default=-1, ge=-1)
    archive_incremental_known_post_limit: int = 20
    archive_sync_interval_seconds: int = Field(default=21600, ge=60)
    archive_schedule_run_immediately_on_start: bool = False
    archive_timezone: str = "Asia/Shanghai"
    archive_media_enabled: bool = True
    archive_media_max_bytes: int = Field(default=0, ge=0)
    task_queue_enabled: bool = True
    task_redis_url: str = "redis://127.0.0.1:6379/0"
    task_crawl_queue_name: str = "archivex:crawl"
    task_media_queue_name: str = "archivex:media"
    task_worker_queue_name: str = "archivex:crawl"
    task_result_ttl_seconds: int = Field(default=604800, ge=60)
    task_retry_count: int = Field(default=5, ge=1)
    task_retry_delay_seconds: int = Field(default=30, ge=1)
    task_retry_max_delay_seconds: int = Field(default=900, ge=1)
    task_sync_timeout_seconds: int = Field(default=1800, ge=60)
    task_media_timeout_seconds: int = Field(default=300, ge=1)
    task_dedupe_ttl_seconds: int = Field(default=3600, ge=60)
    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_auth_token: str = Field(min_length=1)
    web_auth_display_name: str = Field(default="ArchiveX 管理员", min_length=1, max_length=80)
    web_auth_username: str = Field(default="admin", min_length=1, max_length=50)
    web_auth_avatar_url: str | None = None
    web_session_secret: str | None = Field(default=None, min_length=32)
    web_cookie_secure: bool = False
    log_level: str = "INFO"

    @field_validator("archive_incremental_known_post_limit")
    @classmethod
    def validate_known_post_limit(cls, value: int) -> int:
        if value == 0 or value < -1:
            raise ValueError("must be -1 or a positive integer")
        return value

    @field_validator("web_auth_username")
    @classmethod
    def normalize_auth_username(cls, value: str) -> str:
        username = value.strip().lstrip("@")
        if not username:
            raise ValueError("must contain a username")
        return username

    @field_validator("web_auth_avatar_url", mode="before")
    @classmethod
    def normalize_optional_url(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def validate_dedupe_lock_lifetime(self) -> Self:
        reclaim_margin_seconds = 60
        minimum_ttl = max(
            self.task_sync_timeout_seconds,
            self.task_media_timeout_seconds,
            self.task_retry_max_delay_seconds,
        ) + reclaim_margin_seconds
        if self.task_queue_enabled and self.task_dedupe_ttl_seconds < minimum_ttl:
            raise ValueError(
                "task_dedupe_ttl_seconds must be at least "
                f"{minimum_ttl} seconds to cover task execution, retry delay, and reclaim margin"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
