from datetime import UTC, datetime
from collections.abc import AsyncIterator
import json
import sqlite3
import uuid

from fastapi.testclient import TestClient

from archivex.config import Settings
from archivex.main import create_app
from archivex.session import SessionAccountSummary
from archivex.source import SourceAccount, SourcePost
from archivex.storage import ArchiveRepository, MediaInput, PostInput
from archivex.task_dispatcher import TaskSubmission


class FakeSource:
    def __init__(self, accounts=None, posts=None):
        self.accounts = accounts or {}
        self.posts = posts or {}
        self.calls = []

    async def resolve_account(self, username: str):
        self.calls.append(("resolve", username))
        return self.accounts.get(username)

    async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        self.calls.append(("fetch", x_user_id))
        for post in self.posts.get(x_user_id, []):
            yield post


class FakeSessionAccountManager:
    def __init__(self):
        self.proxy = None

    async def list_accounts(self):
        return [SessionAccountSummary(
            username="pni146",
            active=True,
            proxy_configured=self.proxy is not None,
            proxy_url="http://***@proxy.test:8080" if self.proxy else None,
            last_used="2026-08-08T09:45:41+00:00",
            total_requests=661,
        )]

    async def set_proxy(self, username, proxy):
        if username != "pni146":
            return None
        from archivex.session import normalize_http_proxy
        self.proxy = normalize_http_proxy(proxy) if proxy is not None else None
        return (await self.list_accounts())[0]


class FakeTaskDispatcher:
    def __init__(self):
        self.calls = []
        self.media_calls = []

    async def enqueue_account_sync(self, x_user_id, trigger="manual"):
        self.calls.append((x_user_id, trigger))
        return TaskSubmission("task-123", "queued", False)

    async def enqueue_media_download(self, media_id):
        self.media_calls.append(media_id)
        return TaskSubmission("media-task-123", "queued", False)


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        archive_db_path=tmp_path / "archive.sqlite3",
        archive_data_dir=tmp_path / "archive",
        twscrape_session_path=tmp_path / "sessions",
        web_auth_token="test-token",
        web_auth_display_name="Test Admin",
        web_auth_username="test_admin",
        web_auth_avatar_url="https://example.test/admin.jpg",
        task_queue_enabled=False,
        task_dashboard_db_path=tmp_path / "taskiq-dashboard.sqlite3",
    )


def test_archive_api_requires_authentication_and_returns_archived_data(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        account = repository.upsert_account("42", "example", "Example")
        repository.upsert_post(PostInput(
            "100", account.x_user_id, "original", "A useful archive post",
            datetime(2026, 8, 5, 12, tzinfo=UTC), "https://x.com/example/status/100", {
                "id": "100", "replyCount": 2, "retweetCount": 3, "likeCount": 4, "viewCount": 5,
                "rawContent": "A useful archive post https://t.co/media", "lang": "en",
                "isTranslatable": True,
                "user": {
                    "rawDescription": "Archived biography", "location": "Shanghai",
                    "profileImageUrl": "https://example.test/avatar_normal.jpg",
                    "profileBannerUrl": "https://example.test/banner", "followersCount": 12,
                    "friendsCount": 7, "created": "2024-01-01 00:00:00+00:00",
                    "displayname": "Example", "username": "example", "blue": True,
                },
            },
        ))
        media_id = repository.upsert_media(MediaInput("100", "image", "https://example.test/image.jpg"))
        run_id = repository.start_sync_run(account.x_user_id)
        repository.finish_sync_run(run_id, posts_seen=1, posts_new=1, media_new=1, status="success")

        assert client.get("/api/accounts").status_code == 401
        headers = {"Authorization": "Bearer test-token"}

        accounts = client.get("/api/accounts", headers=headers)
        assert accounts.status_code == 200
        assert accounts.json() == [{
            "x_user_id": "42", "current_username": "example", "display_name": "Example",
            "archive_enabled": True,
            "status": "active", "last_sync_at": None, "last_error": None, "post_count": 1,
            "description": "Archived biography", "location": "Shanghai",
            "profile_image_url": "https://example.test/avatar_400x400.jpg",
            "profile_banner_url": "https://example.test/banner/1500x500", "verified": True,
            "followers_count": 12, "following_count": 7,
            "joined_at": "2024-01-01 00:00:00+00:00",
        }]
        posts = client.get("/api/posts?q=useful&has_media=true", headers=headers)
        assert posts.status_code == 200
        assert posts.json()[0]["tweet_id"] == "100"
        assert posts.json()[0]["like_count"] == 4
        assert posts.json()[0]["display_text"] == "A useful archive post"
        assert posts.json()[0]["author_display_name"] == "Example"
        assert posts.json()[0]["author_verified"] is True
        assert posts.json()[0]["language"] == "en"
        detail = client.get("/api/posts/100", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["media"] == [{
            "id": media_id, "media_type": "image", "download_status": "pending", "sha256": None,
            "error": None, "url": None,
        }]
        runs = client.get("/api/sync-runs", headers=headers)
        assert runs.status_code == 200
        assert runs.json()[0]["id"] == run_id
        profile = client.get(f"/api/accounts/{account.x_user_id}", headers=headers).json()
        assert profile["description"] == "Archived biography"
        assert profile["profile_image_url"] == "https://example.test/avatar_400x400.jpg"
        assert profile["profile_banner_url"] == "https://example.test/banner/1500x500"
        assert (profile["following_count"], profile["followers_count"]) == (7, 12)


def test_browser_session_authentication_does_not_expose_the_token(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/auth/session").json() == {"authenticated": False, "user": None}
        assert client.post("/api/auth/session", json={"token": "wrong"}).status_code == 401
        response = client.post("/api/auth/session", json={"token": "test-token"})
        assert response.status_code == 204
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower()
        assert "test-token" not in cookie
        assert client.get("/api/auth/session").json() == {
            "authenticated": True,
            "user": {
                "display_name": "Test Admin",
                "username": "test_admin",
                "avatar_url": "https://example.test/admin.jpg",
            },
        }
        assert client.get("/api/accounts").status_code == 200
        assert client.delete("/api/auth/session").status_code == 204
        assert client.get("/api/accounts").status_code == 401


def test_crawler_account_proxy_can_be_assigned_and_cleared(tmp_path) -> None:
    settings = _settings(tmp_path)
    manager = FakeSessionAccountManager()
    with TestClient(create_app(settings, FakeSource(), manager)) as client:
        headers = {"Authorization": "Bearer test-token"}
        accounts = client.get("/api/crawler-accounts", headers=headers)
        assert accounts.status_code == 200
        assert accounts.json() == [{
            "username": "pni146",
            "active": True,
            "proxy_configured": False,
            "proxy_url": None,
            "last_used": "2026-08-08T09:45:41+00:00",
            "total_requests": 661,
        }]

        updated = client.patch(
            "/api/crawler-accounts/pni146",
            json={"proxy": "http://user:secret@proxy.test:8080"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["proxy_url"] == "http://***@proxy.test:8080"
        assert manager.proxy == "http://user:secret@proxy.test:8080"

        invalid = client.patch(
            "/api/crawler-accounts/pni146",
            json={"proxy": "socks5://proxy.test:1080"},
            headers=headers,
        )
        assert invalid.status_code == 422
        assert "http://host:port" in invalid.json()["detail"]

        cleared = client.patch(
            "/api/crawler-accounts/pni146", json={"proxy": None}, headers=headers
        )
        assert cleared.status_code == 200
        assert cleared.json()["proxy_configured"] is False
        assert client.patch(
            "/api/crawler-accounts/missing", json={"proxy": None}, headers=headers
        ).status_code == 404


def test_archive_api_filters_post_types_and_paginates(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        account = repository.upsert_account("42", "example")
        post_types = ["original", "reply", "quote", "reply", "repost"]
        for index, post_type in enumerate(post_types):
            tweet_id = str(100 + index)
            repository.upsert_post(PostInput(
                tweet_id, account.x_user_id, post_type, f"Post {tweet_id}",
                datetime(2026, 8, 5, 12, index, tzinfo=UTC),
                f"https://x.com/example/status/{tweet_id}", {},
            ))

        headers = {"Authorization": "Bearer test-token"}
        first_posts_page = client.get(
            f"/api/posts?account_x_user_id={account.x_user_id}&exclude_post_type=reply&limit=2",
            headers=headers,
        )
        second_posts_page = client.get(
            f"/api/posts?account_x_user_id={account.x_user_id}&exclude_post_type=reply&limit=2&offset=2",
            headers=headers,
        )
        replies = client.get(
            f"/api/posts?account_x_user_id={account.x_user_id}&post_type=reply&limit=2",
            headers=headers,
        )

        assert [post["tweet_id"] for post in first_posts_page.json()] == ["104", "102"]
        assert [post["tweet_id"] for post in second_posts_page.json()] == ["100"]
        assert [post["tweet_id"] for post in replies.json()] == ["103", "101"]
        assert client.get("/api/posts?post_type=unknown", headers=headers).status_code == 422


def test_archive_api_serves_completed_media_only(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        account = repository.upsert_account("42", "example")
        repository.upsert_post(PostInput(
            "100", account.x_user_id, "original", "Post",
            datetime(2026, 8, 5, tzinfo=UTC), "https://x.com/example/status/100", {},
        ))
        media_path = repository.post_directory("100") / "image.jpg"
        media_path.write_bytes(b"image bytes")
        media_id = repository.upsert_media(MediaInput(
            "100", "image", "https://example.test/image.jpg", download_status="completed",
            local_path=str(media_path.relative_to(settings.archive_data_dir)), sha256="hash",
        ))
        response = client.get(f"/api/media/{media_id}", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert response.content == b"image bytes"


def test_account_management_resolves_once_and_then_uses_x_user_id(tmp_path) -> None:
    settings = _settings(tmp_path)
    x_user_id = "9007199254740993"
    source = FakeSource({
        "alice": SourceAccount(
            x_user_id, "alice", "Alice", "https://example.test/alice_normal.jpg", "Bio"
        )
    })
    with TestClient(create_app(settings, source)) as client:
        headers = {"Authorization": "Bearer test-token"}
        resolved = client.post(
            "/api/accounts/resolve",
            json={"query": "https://x.com/alice"},
            headers=headers,
        )
        assert resolved.status_code == 200
        assert resolved.json() == {
            "x_user_id": x_user_id,
            "current_username": "alice",
            "display_name": "Alice",
            "profile_image_url": "https://example.test/alice_400x400.jpg",
            "description": "Bio",
            "already_archived": False,
            "archive_enabled": None,
        }

        added = client.post(
            "/api/accounts",
            json={
                "x_user_id": x_user_id,
                "current_username": "alice",
                "display_name": "Alice",
            },
            headers=headers,
        )
        assert added.status_code == 201
        assert added.json()["x_user_id"] == x_user_id
        assert added.json()["archive_enabled"] is True
        assert source.calls == [("resolve", "alice"), ("fetch", x_user_id)]

        runs = client.get(
            f"/api/sync-runs?account_x_user_id={x_user_id}", headers=headers
        )
        assert runs.status_code == 200
        assert [run["status"] for run in runs.json()] == ["success"]

        paused = client.patch(
            f"/api/accounts/{x_user_id}",
            json={"archive_enabled": False},
            headers=headers,
        )
        assert paused.status_code == 200
        assert paused.json()["archive_enabled"] is False
        assert paused.json()["status"] == "paused"

        synced = client.post(f"/api/accounts/{x_user_id}/sync", headers=headers)
        assert synced.status_code == 202
        assert synced.json()["state"] == "success"
        assert synced.json()["task_id"] == f"inline:{x_user_id}"
        assert synced.json()["x_user_id"] == x_user_id

        history = client.get(
            f"/api/accounts/{x_user_id}/username-history", headers=headers
        )
        assert [item["username"] for item in history.json()] == ["alice"]

    assert source.calls == [
        ("resolve", "alice"),
        ("fetch", x_user_id),
        ("fetch", x_user_id),
    ]


def test_manual_sync_enqueues_without_waiting_for_the_source(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource()
    dispatcher = FakeTaskDispatcher()
    with TestClient(create_app(
        settings,
        source,
        FakeSessionAccountManager(),
        dispatcher,
    )) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("42", "example")
        response = client.post(
            "/api/accounts/42/sync",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "x_user_id": "42",
        "task_id": "task-123",
        "state": "queued",
        "duplicate": False,
    }
    assert dispatcher.calls == [("42", "manual")]
    assert source.calls == []


def test_integrated_task_center_lists_and_reruns_tasks(tmp_path) -> None:
    settings = _settings(tmp_path)
    task_id = uuid.uuid4()
    with sqlite3.connect(settings.task_dashboard_db_path) as connection:
        connection.execute(
            """CREATE TABLE tasks (
                id CHAR(32) PRIMARY KEY, name TEXT NOT NULL, status INTEGER NOT NULL,
                worker TEXT NOT NULL, args JSON NOT NULL, kwargs JSON NOT NULL,
                labels JSON NOT NULL, result JSON, error TEXT, queued_at DATETIME,
                started_at DATETIME, finished_at DATETIME
            )"""
        )
        connection.execute(
            """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id.hex,
                "archivex.sync_account",
                2,
                "archivex:crawl",
                json.dumps(["42", "manual"]),
                "{}",
                "{}",
                None,
                "network unavailable",
                "2026-08-08 12:00:00",
                "2026-08-08 12:00:01",
                "2026-08-08 12:00:03",
            ),
        )

    dispatcher = FakeTaskDispatcher()
    with TestClient(create_app(
        settings,
        FakeSource(),
        FakeSessionAccountManager(),
        dispatcher,
    )) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("42", "example")
        headers = {"Authorization": "Bearer test-token"}

        tasks = client.get("/api/task-center/tasks?status=failure", headers=headers)
        assert tasks.status_code == 200
        assert tasks.json()["total"] == 1
        assert tasks.json()["items"][0]["id"] == str(task_id)
        assert tasks.json()["items"][0]["duration_ms"] == 2000

        searched = client.get(f"/api/task-center/tasks?q={task_id}", headers=headers)
        assert searched.status_code == 200
        assert searched.json()["total"] == 1

        detail = client.get(f"/api/task-center/tasks/{task_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "failure"

        rerun = client.post(
            f"/api/task-center/tasks/{task_id}/rerun", headers=headers
        )
        assert rerun.status_code == 202
        assert rerun.json()["task_id"] == "task-123"

        queued_task_id = uuid.uuid4()
        with sqlite3.connect(settings.task_dashboard_db_path) as connection:
            connection.execute(
                """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    queued_task_id.hex, "archivex.sync_account", 3, "archivex:crawl",
                    json.dumps(["999", "manual"]), "{}", "{}", None, None,
                    "2026-08-08 12:00:04", None, None,
                ),
            )
        blocked_rerun = client.post(
            f"/api/task-center/tasks/{queued_task_id}/rerun", headers=headers
        )
        assert blocked_rerun.status_code == 409

        abandoned_task_id = uuid.uuid4()
        with sqlite3.connect(settings.task_dashboard_db_path) as connection:
            connection.execute(
                """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    abandoned_task_id.hex, "archivex.sync_account", 4, "archivex:crawl",
                    json.dumps(["42", "manual"]), "{}", "{}", None, "publish failed",
                    "2026-08-08 12:00:05", None, "2026-08-08 12:00:06",
                ),
            )
        cleared = client.delete("/api/task-center/tasks/abandoned", headers=headers)
        assert cleared.status_code == 200
        assert cleared.json() == {"deleted": 1}
        assert client.get(
            f"/api/task-center/tasks/{abandoned_task_id}", headers=headers
        ).status_code == 404

        schedules = client.get("/api/task-center/schedules", headers=headers)
        assert schedules.status_code == 200
        assert schedules.json()[0]["enabled_accounts"] == 1

        run_schedule = client.post(
            "/api/task-center/schedules/enabled-account-sync/run", headers=headers
        )
        assert run_schedule.status_code == 202
        assert run_schedule.json() == {"queued": 1, "duplicates": 0}

        repository.upsert_post(PostInput(
            "100", "42", "original", "Post with media",
            datetime(2026, 8, 8, tzinfo=UTC), "https://x.com/example/status/100", {},
        ))
        pending_media_id = repository.upsert_media(MediaInput(
            "100", "image", "https://example.test/pending.jpg",
        ))
        completed_media_id = repository.upsert_media(MediaInput(
            "100", "image", "https://example.test/completed.jpg",
            download_status="completed",
        ))
        repository.upsert_account("85", "retrying")
        with sqlite3.connect(settings.task_dashboard_db_path) as connection:
            for name, args, task_status, labels, started_at in [
                ("archivex.sync_account", ["42", "older"], 2, {}, "2026-08-08 11:00:00"),
                ("archivex.sync_account", ["84", "older"], 2, {}, "2026-08-08 12:10:00"),
                ("archivex.sync_account", ["84", "manual"], 1, {}, "2026-08-08 12:11:00"),
                (
                    "archivex.sync_account", ["85", "scheduled"], 2,
                    {"max_retries": "5", "_retries": "2"}, "2026-08-08 12:12:00",
                ),
                ("archivex.download_media", [pending_media_id], 2, {}, "2026-08-08 13:00:00"),
                ("archivex.download_media", [completed_media_id], 2, {}, "2026-08-08 13:01:00"),
                ("archivex.schedule_enabled_accounts", [], 2, {}, "2026-08-08 13:02:00"),
                ("archivex.unknown_task", ["target"], 2, {}, "2026-08-08 13:03:00"),
            ]:
                connection.execute(
                    """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex, name, task_status, "archivex:crawl",
                        json.dumps(args), "{}", json.dumps(labels), None,
                        "failed" if task_status == 2 else None,
                        started_at, started_at, started_at,
                    ),
                )

        current_failures = client.get(
            "/api/task-center/tasks?status=failure", headers=headers
        )
        assert current_failures.status_code == 200
        assert current_failures.json()["total"] == 4
        assert current_failures.json()["counts"]["failure"] == 4
        assert sorted(task["name"] for task in current_failures.json()["items"]) == [
            "archivex.download_media",
            "archivex.schedule_enabled_accounts",
            "archivex.sync_account",
            "archivex.unknown_task",
        ]

        retry_failures = client.post(
            "/api/task-center/failures/retry", headers=headers
        )
        assert retry_failures.status_code == 202
        assert retry_failures.json() == {
            "queued": 4,
            "duplicates": 0,
            "skipped_resolved": 2,
            "automatic_retrying": 1,
            "unsupported": 1,
            "failed": 0,
        }

    assert dispatcher.calls == [
        ("42", "rerun"),
        ("42", "schedule_manual"),
        ("42", "schedule_manual"),
        ("85", "schedule_manual"),
        ("42", "failure_retry"),
    ]
    assert dispatcher.media_calls == [pending_media_id]
