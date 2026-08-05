from pathlib import Path

import pytest
from pydantic import ValidationError

from archivex.config import Settings


def test_required_settings_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_accounts_are_normalized() -> None:
    settings = Settings(
        _env_file=None,
        archive_accounts=" @first,second, ",
        archive_db_path="/tmp/archive.sqlite3",
        archive_data_dir="/tmp/archive",
        web_auth_token="test-token",
    )

    assert settings.archive_accounts == ["first", "second"]

