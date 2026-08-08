from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded once when the application process starts."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twscrape_session_path: Path = Path("/data/twscrape")
    archive_db_path: Path = Path("/data/archive.sqlite3")
    archive_data_dir: Path = Path("/data/archive")
    archive_initial_post_limit: int = Field(default=-1, ge=-1)
    archive_incremental_known_post_limit: int = 20
    archive_sync_interval_seconds: int = Field(default=21600, ge=60)
    archive_timezone: str = "Asia/Shanghai"
    archive_media_enabled: bool = True
    archive_media_max_bytes: int = Field(default=0, ge=0)
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
