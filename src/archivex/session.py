from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence

from twscrape import AccountsPool


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
