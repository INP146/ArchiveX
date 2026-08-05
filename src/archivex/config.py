from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded once when the application process starts."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    archive_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    twscrape_session_path: Path = Path("/data/twscrape")
    archive_db_path: Path
    archive_data_dir: Path
    archive_initial_post_limit: int = Field(default=-1, ge=-1)
    archive_incremental_known_post_limit: int = 20
    archive_sync_interval_seconds: int = Field(default=21600, ge=60)
    archive_timezone: str = "Asia/Shanghai"
    archive_media_enabled: bool = True
    archive_media_max_bytes: int = Field(default=0, ge=0)
    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_auth_token: str = Field(min_length=1)
    web_session_secret: str | None = Field(default=None, min_length=32)
    web_cookie_secure: bool = False
    log_level: str = "INFO"

    @field_validator("archive_accounts", mode="before")
    @classmethod
    def split_accounts(cls, value: str | list[str] | None) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        return [account.strip().lstrip("@") for account in value.split(",") if account.strip()]

    @field_validator("archive_incremental_known_post_limit")
    @classmethod
    def validate_known_post_limit(cls, value: int) -> int:
        if value == 0 or value < -1:
            raise ValueError("must be -1 or a positive integer")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
