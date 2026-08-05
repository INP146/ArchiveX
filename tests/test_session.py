import asyncio
import subprocess

import pytest

from archivex.session import (
    cookies_from_clipboard,
    import_cookies,
    session_database_path,
    session_status,
)


class Account:
    def __init__(self, active: bool) -> None:
        self.active = active


class FakePool:
    def __init__(self) -> None:
        self.accounts = {}

    async def get_account(self, username):
        return self.accounts.get(username)

    async def add_account_cookies(self, username, cookies):
        self.accounts[username] = Account(active=cookies == "valid")

    async def delete_accounts(self, username):
        self.accounts.pop(username)

    async def get_all(self):
        return list(self.accounts.values())


def test_session_database_path_accepts_a_directory_or_file(tmp_path) -> None:
    assert session_database_path(tmp_path / "sessions") == tmp_path / "sessions" / "accounts.db"
    assert session_database_path(tmp_path / "accounts.sqlite") == tmp_path / "accounts.sqlite"


def test_cookie_import_requires_explicit_replacement() -> None:
    pool = FakePool()
    assert asyncio.run(import_cookies(pool, "login", "valid", replace=False))
    with pytest.raises(ValueError, match="--replace"):
        asyncio.run(import_cookies(pool, "login", "valid", replace=False))
    assert not asyncio.run(import_cookies(pool, "login", "invalid", replace=True))
    assert asyncio.run(session_status(pool)) == (1, 0)


def test_cookies_from_clipboard(monkeypatch) -> None:
    clipboard_values = iter(["the copied command", "auth_token=a; ct0=b\n"])

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=next(clipboard_values))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("archivex.session.time.sleep", lambda _: None)

    assert cookies_from_clipboard() == "auth_token=a; ct0=b"
