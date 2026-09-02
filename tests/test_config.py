from pathlib import Path

import pytest
from pydantic import ValidationError

from archivex.config import Settings


def test_required_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "value",
    [" @archive_admin ", " @ archive_admin "],
)
def test_web_auth_username_is_normalized(value: str) -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path="/tmp/archive.sqlite3",
        archive_data_dir="/tmp/archive",
        web_auth_token="test-token",
        web_auth_username=value,
    )

    assert settings.web_auth_username == "archive_admin"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_web_auth_avatar_url_is_normalized_to_none(value: str) -> None:
    settings = Settings(
        _env_file=None,
        web_auth_token="test-token",
        web_auth_avatar_url=value,
    )

    assert settings.web_auth_avatar_url is None


def test_web_auth_avatar_url_whitespace_is_trimmed() -> None:
    settings = Settings(
        _env_file=None,
        web_auth_token="test-token",
        web_auth_avatar_url=" https://example.test/avatar.png ",
    )

    assert settings.web_auth_avatar_url == "https://example.test/avatar.png"


def test_log_level_is_normalized_and_validated() -> None:
    settings = Settings(
        _env_file=None,
        web_auth_token="test-token",
        log_level=" debug ",
    )

    assert settings.log_level == "DEBUG"

    with pytest.raises(ValidationError, match="unknown logging level"):
        Settings(
            _env_file=None,
            web_auth_token="test-token",
            log_level="verbose",
        )


def test_twscrape_pool_wait_settings_are_bounded() -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path="/tmp/archive.sqlite3",
        archive_data_dir="/tmp/archive",
        web_auth_token="test-token",
        twscrape_wait_timeout_seconds=0,
        twscrape_wait_interval_seconds=0.25,
    )

    assert settings.twscrape_wait_timeout_seconds == 0
    assert settings.twscrape_wait_interval_seconds == 0.25

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            archive_db_path="/tmp/archive.sqlite3",
            archive_data_dir="/tmp/archive",
            web_auth_token="test-token",
            twscrape_wait_interval_seconds=0,
        )


def test_dedupe_ttl_must_cover_task_timeout_retry_and_reclaim_margin() -> None:
    with pytest.raises(ValidationError, match="task_dedupe_ttl_seconds must be at least 360"):
        Settings(
            _env_file=None,
            archive_db_path="/tmp/archive.sqlite3",
            archive_data_dir="/tmp/archive",
            web_auth_token="test-token",
            task_sync_timeout_seconds=60,
            task_media_timeout_seconds=300,
            task_retry_max_delay_seconds=30,
            task_dedupe_ttl_seconds=359,
        )

    settings = Settings(
        _env_file=None,
        archive_db_path="/tmp/archive.sqlite3",
        archive_data_dir="/tmp/archive",
        web_auth_token="test-token",
        task_sync_timeout_seconds=60,
        task_media_timeout_seconds=300,
        task_retry_max_delay_seconds=30,
        task_dedupe_ttl_seconds=360,
    )
    assert settings.task_dedupe_ttl_seconds == 360
