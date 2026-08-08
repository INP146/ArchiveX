from pathlib import Path

import pytest
from pydantic import ValidationError

from archivex.config import Settings


def test_required_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_web_auth_username_is_normalized() -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path="/tmp/archive.sqlite3",
        archive_data_dir="/tmp/archive",
        web_auth_token="test-token",
        web_auth_username=" @archive_admin ",
    )

    assert settings.web_auth_username == "archive_admin"
