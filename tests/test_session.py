import asyncio
import subprocess
from datetime import UTC, datetime

import pytest
from twscrape import AccountsPool

from archivex.session import (
    TwscrapeSessionAccountManager,
    cookies_from_clipboard,
    import_cookies,
    mask_http_proxy,
    normalize_http_proxy,
    session_database_path,
    session_status,
)


class Account:
    def __init__(self, active: bool) -> None:
        self.active = active
        self.username = "login"
        self.proxy = None
        self.last_used = datetime(2026, 8, 8, tzinfo=UTC)
        self.stats = {"timeline": 3, "profile": 2}


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

    async def save(self, account):
        self.accounts[account.username] = account


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


def test_http_proxy_is_validated_and_credentials_are_masked() -> None:
    assert normalize_http_proxy(" http://user:secret@127.0.0.1:8080/ ") == (
        "http://user:secret@127.0.0.1:8080"
    )
    assert mask_http_proxy("http://user:secret@127.0.0.1:8080") == (
        "http://***@127.0.0.1:8080"
    )
    with pytest.raises(ValueError, match="http://host:port"):
        normalize_http_proxy("socks5://127.0.0.1:1080")
    with pytest.raises(ValueError, match="path"):
        normalize_http_proxy("http://127.0.0.1:8080/path")


def test_session_account_manager_assigns_proxy_without_replacing_account() -> None:
    pool = FakePool()
    account = Account(active=True)
    pool.accounts[account.username] = account
    manager = object.__new__(TwscrapeSessionAccountManager)
    manager.pool = pool

    updated = asyncio.run(manager.set_proxy("login", "http://user:secret@proxy.test:8080"))

    assert updated is not None
    assert updated.proxy_configured is True
    assert updated.proxy_url == "http://***@proxy.test:8080"
    assert updated.total_requests == 5
    assert pool.accounts["login"] is account
    assert account.proxy == "http://user:secret@proxy.test:8080"

    cleared = asyncio.run(manager.set_proxy("login", None))
    assert cleared is not None and cleared.proxy_configured is False
    assert asyncio.run(manager.set_proxy("missing", None)) is None


def test_session_account_manager_updates_real_twscrape_database(tmp_path) -> None:
    pool = AccountsPool(str(tmp_path / "accounts.db"))
    manager = object.__new__(TwscrapeSessionAccountManager)
    manager.pool = pool

    async def update_proxy() -> None:
        await pool.add_account_cookies("archivex_test_login", "auth_token=a; ct0=b")
        updated = await manager.set_proxy("archivex_test_login", "http://proxy.test:8080")
        assert updated is not None
        assert updated.proxy_url == "http://proxy.test:8080"
        stored = await pool.get_account("archivex_test_login")
        assert stored is not None
        assert stored.proxy == "http://proxy.test:8080"

    asyncio.run(update_proxy())


def test_session_account_manager_imports_cookies(tmp_path) -> None:
    manager = TwscrapeSessionAccountManager(tmp_path / "sessions")

    async def import_session() -> None:
        account = await manager.import_cookies("archivex_test_login", "auth_token=a; ct0=b")
        assert account.username == "archivex_test_login"
        assert account.active is True
        assert await manager.list_accounts() == [account]

    asyncio.run(import_session())
