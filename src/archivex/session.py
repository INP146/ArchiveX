from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from twscrape import AccountsPool
from twscrape.db import execute as execute_twscrape_query


@dataclass(frozen=True)
class SessionAccountSummary:
    username: str
    active: bool
    proxy_configured: bool
    proxy_url: str | None
    last_used: str | None
    total_requests: int


class SessionAccountManager(Protocol):
    async def list_accounts(self) -> list[SessionAccountSummary]: ...

    async def import_cookies(
        self, username: str, cookies: str, replace: bool = False
    ) -> SessionAccountSummary: ...

    async def set_proxy(self, username: str, proxy: str | None) -> SessionAccountSummary | None: ...


class TwscrapeSessionAccountManager:
    def __init__(self, session_path: Path) -> None:
        database_path = session_database_path(session_path)
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_permissions(database_path.parent)
        self.pool = AccountsPool(str(database_path))

    async def list_accounts(self) -> list[SessionAccountSummary]:
        accounts = await self.pool.get_all()
        return sorted((_session_account_summary(account) for account in accounts),
                      key=lambda account: account.username.casefold())

    async def import_cookies(
        self, username: str, cookies: str, replace: bool = False
    ) -> SessionAccountSummary:
        await import_cookies(self.pool, username, cookies, replace)
        account = await self.pool.get_account(username)
        if account is None:
            raise ValueError("session was not saved")
        return _session_account_summary(account)

    async def set_proxy(self, username: str,
                        proxy: str | None) -> SessionAccountSummary | None:
        return await set_pool_account_proxy(self.pool, username, proxy)


def session_database_path(session_path: Path) -> Path:
    return session_path if session_path.suffix else session_path / "accounts.db"


async def import_cookies(pool: AccountsPool, username: str, cookies: str, replace: bool) -> bool:
    existing = await pool.get_account(username)
    if existing is not None:
        if not replace:
            raise ValueError("a session for this username already exists; rerun with --replace")
        await pool.delete_accounts(username)
    await pool.add_account_cookies(username, cookies)
    account = await pool.get_account(username)
    return bool(account and account.active)


async def login_with_credentials(
    pool: AccountsPool,
    username: str,
    password: str,
    email: str,
    email_password: str,
    replace: bool,
) -> bool:
    existing = await pool.get_account(username)
    if existing is not None:
        if not replace:
            raise ValueError("a session for this username already exists; rerun with --replace")
        await pool.delete_accounts(username)
    await pool.add_account(username, password, email, email_password)
    result = await pool.login_all([username])
    return result["success"] == 1


async def session_status(pool: AccountsPool) -> tuple[int, int]:
    accounts = await pool.get_all()
    return len(accounts), sum(account.active for account in accounts)


async def set_pool_account_proxy(
    pool: AccountsPool, username: str, proxy: str | None
) -> SessionAccountSummary | None:
    account = await pool.get_account(username)
    if account is None:
        return None
    normalized = normalize_http_proxy(proxy) if proxy is not None else None
    database_path = getattr(pool, "_db_file", None)
    if database_path is not None:
        # Update only the proxy so an in-flight crawler cannot lose lock or stats changes.
        await execute_twscrape_query(
            database_path,
            "UPDATE accounts SET proxy = :proxy WHERE username = :username",
            {"proxy": normalized, "username": username},
        )
        account = await pool.get_account(username)
    else:
        account.proxy = normalized
        await pool.save(account)
    return _session_account_summary(account)


def normalize_http_proxy(value: str) -> str:
    proxy = value.strip()
    if not proxy or any(character.isspace() for character in proxy):
        raise ValueError("HTTP proxy URL is empty or contains whitespace")
    parsed = urlsplit(proxy)
    if parsed.scheme.lower() != "http" or parsed.hostname is None:
        raise ValueError("HTTP proxy must use http://host:port")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("HTTP proxy has an invalid port") from exc
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("HTTP proxy URL cannot contain a path, query, or fragment")
    return urlunsplit(("http", parsed.netloc, "", "", ""))


def mask_http_proxy(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    credentials = "***@" if parsed.username is not None else ""
    return urlunsplit((parsed.scheme or "http", f"{credentials}{host}{port}", "", "", ""))


def _session_account_summary(account: object) -> SessionAccountSummary:
    proxy = getattr(account, "proxy", None)
    last_used = getattr(account, "last_used", None)
    stats = getattr(account, "stats", {})
    return SessionAccountSummary(
        username=str(getattr(account, "username")),
        active=bool(getattr(account, "active", False)),
        proxy_configured=bool(proxy),
        proxy_url=mask_http_proxy(proxy),
        last_used=last_used.isoformat() if last_used else None,
        total_requests=sum(value for value in stats.values() if isinstance(value, int)),
    )


def cookies_from_clipboard(timeout_seconds: float = 120, poll_interval: float = 0.25) -> str:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            result = subprocess.run(
                ["pbpaste"],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("--from-clipboard requires the macOS pbpaste command") from exc

        cookies = result.stdout.strip()
        if "auth_token=" in cookies and "ct0=" in cookies:
            return cookies
        if time.monotonic() >= deadline:
            raise TimeoutError("no X cookies appeared on the clipboard within 120 seconds")
        time.sleep(poll_interval)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and inspect ArchiveX twscrape sessions.")
    parser.add_argument(
        "--session-path",
        type=Path,
        default=Path(os.environ.get("TWSCRAPE_SESSION_PATH", "/data/twscrape")),
        help="twscrape database file or a directory containing accounts.db",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    cookies = commands.add_parser("cookies")
    cookies.add_argument("--username", required=True, help="Your X login username")
    cookies.add_argument("--replace", action="store_true", help="Replace this existing session")
    cookies.add_argument(
        "--from-clipboard",
        action="store_true",
        help="Wait for X cookies to be copied to the macOS clipboard",
    )

    login = commands.add_parser("login")
    login.add_argument("--username", required=True, help="Your X login username")
    login.add_argument("--replace", action="store_true", help="Replace this existing session")

    proxy = commands.add_parser("proxy", help="Set the HTTP proxy for a twscrape account")
    proxy.add_argument("--username", required=True, help="Existing twscrape account username")
    proxy_action = proxy.add_mutually_exclusive_group()
    proxy_action.add_argument("--url", help="HTTP proxy URL (prefer the interactive prompt)")
    proxy_action.add_argument("--clear", action="store_true", help="Remove the assigned proxy")

    commands.add_parser("status")
    args = parser.parse_args(argv)
    database_path = session_database_path(args.session_path)
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(database_path.parent)
    pool = AccountsPool(str(database_path))

    try:
        if args.command == "cookies":
            if args.from_clipboard:
                print("Waiting for X cookies on the clipboard (up to 120 seconds)...")
            cookies_value = (
                cookies_from_clipboard()
                if args.from_clipboard
                else input("X cookies (auth_token=...; ct0=...): ").strip()
            )
            if not cookies_value:
                raise ValueError("cookies are empty")
            active = asyncio.run(import_cookies(pool, args.username, cookies_value, args.replace))
            _restrict_permissions(database_path)
            print("Session saved." if active else "Session was saved but is not active; check the cookies.")
            return 0 if active else 1
        if args.command == "login":
            password = input("X password: ")
            email = input("Account email: ").strip()
            email_password = input("Email password: ")
            active = asyncio.run(
                login_with_credentials(
                    pool,
                    args.username,
                    password,
                    email,
                    email_password,
                    args.replace,
                )
            )
            _restrict_permissions(database_path)
            print("Session saved." if active else "Login did not create an active session.")
            return 0 if active else 1
        if args.command == "proxy":
            proxy_value = None if args.clear else normalize_http_proxy(
                args.url or getpass.getpass("HTTP proxy URL: ")
            )
            updated = asyncio.run(set_pool_account_proxy(pool, args.username, proxy_value))
            if updated is None:
                raise ValueError("twscrape account not found")
            _restrict_permissions(database_path)
            print(
                "Proxy cleared."
                if proxy_value is None
                else f"Proxy saved: {mask_http_proxy(proxy_value)}"
            )
            return 0
        total, active = asyncio.run(session_status(pool))
        _restrict_permissions(database_path)
        print(f"Sessions: {total}; active: {active}")
        return 0
    except Exception as exc:
        print(f"Session setup failed ({exc.__class__.__name__}).")
        return 1


def _restrict_permissions(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600 if path.is_file() else 0o700)


if __name__ == "__main__":
    raise SystemExit(main())
