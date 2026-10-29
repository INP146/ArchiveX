from datetime import UTC, datetime
from collections.abc import AsyncIterator
import uuid

from fastapi.testclient import TestClient

from archivex.config import Settings
from archivex.main import create_app
from archivex.session import SessionAccountSummary
from archivex.source import SourceAccount, SourcePost
from archivex.storage import ArchiveRepository, MediaInput, PostInput, RepostInput, initialize_storage
from archivex.task_center import (
    TASK_OBSERVED_ACCOUNT_ID_LABEL,
    TASK_MEDIA_ID_LABEL,
    TASK_PARENT_ID_LABEL,
    TASK_TRIGGER_LABEL,
    TaskCenterRepository,
)
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
            username="archivex_test_login",
            active=True,
            proxy_configured=self.proxy is not None,
            proxy_url="http://***@proxy.test:8080" if self.proxy else None,
            last_used="2026-08-08T09:45:41+00:00",
            total_requests=661,
        )]

    async def set_proxy(self, username, proxy):
        if username != "archivex_test_login":
            return None
        from archivex.session import normalize_http_proxy
        self.proxy = normalize_http_proxy(proxy) if proxy is not None else None
        return (await self.list_accounts())[0]

    async def import_cookies(self, username, cookies, replace=False):
        if username != "archivex_test_login":
            if not replace:
                raise ValueError("a session for this username already exists; rerun with --replace")
        if cookies != "auth_token=a; ct0=b":
            raise ValueError("X cookies could not be imported")
        self.proxy = None
        return (await self.list_accounts())[0]


class FakeTaskDispatcher:
    def __init__(self):
        self.calls = []
        self.media_calls = []
        self.full_scan_calls = []

    async def enqueue_account_sync(self, x_user_id, trigger="manual", retry_of=None, *, full_scan=False):
        self.calls.append((x_user_id, trigger, retry_of))
        if full_scan:
            self.full_scan_calls.append((x_user_id, trigger, retry_of))
        return TaskSubmission("task-123", "queued", False)

    async def enqueue_media_download(
        self, media_id, retry_of=None, parent_task_id=None
    ):
        self.media_calls.append((media_id, retry_of, parent_task_id))
        return TaskSubmission("media-task-123", "queued", False)


def test_single_and_all_account_full_scans_preserve_paused_accounts_and_mode(tmp_path):
    settings = _settings(tmp_path)
    dispatcher = FakeTaskDispatcher()
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), dispatcher)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("42", "example")
        repository.upsert_account("43", "paused")
        repository.set_account_enabled("43", False)
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/api/accounts/full-scan").status_code == 401
        single = client.post("/api/accounts/42/sync", json={"full_scan": True}, headers=headers)
        assert single.status_code == 202
        assert dispatcher.full_scan_calls == [("42", "manual_full_scan", None)]

        batch = client.post("/api/accounts/full-scan", headers=headers)
        assert batch.status_code == 202
        assert batch.json()["queued"] == 2
        assert {task["x_user_id"] for task in batch.json()["tasks"]} == {"42", "43"}
        assert repository.get_account("43").archive_enabled is False

        import uuid
        task_id = str(uuid.uuid4())
        lifecycle = TaskCenterRepository(settings.archive_db_path, 3600, "archivex:crawl")
        lifecycle.record_finished(
            task_id, {TASK_OBSERVED_ACCOUNT_ID_LABEL: "43", "full_scan": True},
            result=None, error="request failed", name="archivex.sync_account",
            worker="archivex:crawl", args=["43", "manual_full_scan"], kwargs={"full_scan": True},
        )
        rerun = client.post(f"/api/task-center/tasks/{task_id}/rerun", headers=headers)
        assert rerun.status_code == 202
        assert dispatcher.full_scan_calls[-1] == ("43", "rerun", task_id)
        retry = client.post("/api/task-center/failures/retry", headers=headers)
        assert retry.status_code == 202
        assert dispatcher.full_scan_calls[-1] == ("43", "failure_retry", task_id)


def test_all_account_full_scan_reports_busy_and_failed_submissions(tmp_path):
    settings = _settings(tmp_path)
    class Dispatcher(FakeTaskDispatcher):
        async def enqueue_account_sync(self, x_user_id, trigger="manual", retry_of=None, *, full_scan=False):
            assert full_scan
            if x_user_id == "43":
                return TaskSubmission("busy-task", "queued", True)
            if x_user_id == "44":
                raise RuntimeError("queue unavailable")
            return TaskSubmission("new-task", "queued", False)

    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), Dispatcher())) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        for identifier in ("42", "43", "44"):
            repository.upsert_account(identifier, f"user{identifier}")
        response = client.post("/api/accounts/full-scan", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 202
        assert {key: response.json()[key] for key in ("queued", "duplicates", "failed")} == {
            "queued": 1, "duplicates": 1, "failed": 1,
        }


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
    )


def test_account_archive_policy_api_is_partial_persistent_and_authenticated(tmp_path):
    settings = _settings(tmp_path)
    dispatcher = FakeTaskDispatcher()
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), dispatcher)) as client:
        headers = {"Authorization": "Bearer test-token"}
        body = {"x_user_id": "42", "current_username": "example", "archive_reposts": False}
        created = client.post("/api/accounts", json=body, headers=headers)
        assert created.status_code == 201
        assert created.json()["archive_reposts"] is False
        assert created.json()["archive_originals"] is True
        assert created.json()["archive_replies"] is True
        assert dispatcher.calls
        assert client.patch("/api/accounts/42", json={"archive_replies": False}).status_code == 401
        updated = client.patch("/api/accounts/42", json={"archive_replies": False}, headers=headers)
        assert updated.status_code == 200
        assert updated.json()["archive_enabled"] is True
        assert updated.json()["archive_originals"] is True
        assert updated.json()["archive_reposts"] is False
        assert updated.json()["archive_replies"] is False
        paused = client.patch("/api/accounts/42", json={"archive_enabled": False}, headers=headers)
        assert paused.json()["status"] == "paused"
        changed_paused = client.patch("/api/accounts/42", json={"archive_originals": False}, headers=headers)
        assert changed_paused.json()["status"] == "paused"
        assert changed_paused.json()["archive_enabled"] is False
        assert client.get("/api/accounts", headers=headers).json()[0]["archive_reposts"] is False
        assert client.get("/api/accounts/42", headers=headers).json()["archive_replies"] is False
        assert client.patch("/api/accounts/42", json={"archive_reposts": None}, headers=headers).status_code == 422
        assert client.patch("/api/accounts/42", json={"archive_reposts": "invalid"}, headers=headers).status_code == 422
        assert client.patch("/api/accounts/99", json={"archive_reposts": False}, headers=headers).status_code == 404
        # Older clients omit the new fields when re-enabling; keep the stored choices.
        enabled = client.post("/api/accounts", json={"x_user_id": "42", "current_username": "example"}, headers=headers)
        assert enabled.json()["archive_enabled"] is True
        assert enabled.json()["archive_reposts"] is False
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), dispatcher)) as client:
        assert client.get("/api/accounts/42", headers=headers).json()["archive_reposts"] is False


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
        repository.observe_post("42", "100")
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
            "archive_originals": True, "archive_replies": True, "archive_reposts": True,
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
            "username": "archivex_test_login",
            "active": True,
            "proxy_configured": False,
            "proxy_url": None,
            "last_used": "2026-08-08T09:45:41+00:00",
            "total_requests": 661,
        }]

        updated = client.patch(
            "/api/crawler-accounts/archivex_test_login",
            json={"proxy": "http://user:secret@proxy.test:8080"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["proxy_url"] == "http://***@proxy.test:8080"
        assert manager.proxy == "http://user:secret@proxy.test:8080"

        invalid = client.patch(
            "/api/crawler-accounts/archivex_test_login",
            json={"proxy": "socks5://proxy.test:1080"},
            headers=headers,
        )
        assert invalid.status_code == 422
        assert "http://host:port" in invalid.json()["detail"]

        cleared = client.patch(
            "/api/crawler-accounts/archivex_test_login", json={"proxy": None}, headers=headers
        )
        assert cleared.status_code == 200
        assert cleared.json()["proxy_configured"] is False
        assert client.patch(
            "/api/crawler-accounts/missing", json={"proxy": None}, headers=headers
        ).status_code == 404


def test_crawler_account_cookies_can_be_imported_from_web(tmp_path) -> None:
    settings = _settings(tmp_path)
    manager = FakeSessionAccountManager()
    with TestClient(create_app(settings, FakeSource(), manager)) as client:
        headers = {"Authorization": "Bearer test-token"}
        imported = client.post(
            "/api/crawler-accounts",
            json={"username": "archivex_test_login", "cookies": "auth_token=a; ct0=b"},
            headers=headers,
        )
        assert imported.status_code == 201
        assert imported.json()["username"] == "archivex_test_login"
        assert client.post(
            "/api/crawler-accounts",
            json={"username": "bad user", "cookies": "auth_token=a; ct0=b"},
            headers=headers,
        ).status_code == 422


def test_archive_api_filters_post_types_and_paginates(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        account = repository.upsert_account("42", "example")
        post_types = ["original", "reply", "quote", "reply", "repost"]
        for index, post_type in enumerate(post_types):
            tweet_id = str(100 + index)
            if post_type == "repost":
                repository.upsert_repost(RepostInput(tweet_id, "100", account.x_user_id,
                    datetime(2026, 8, 5, 12, index, tzinfo=UTC), f"https://x.com/example/status/{tweet_id}"))
                repository.observe_repost(account.x_user_id, tweet_id)
            else:
                repository.upsert_post(PostInput(
                    tweet_id, account.x_user_id, post_type, f"Post {tweet_id}",
                    datetime(2026, 8, 5, 12, index, tzinfo=UTC),
                    f"https://x.com/example/status/{tweet_id}", {},
                ))
                repository.observe_post(account.x_user_id, tweet_id)


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
            "archive_originals": True, "archive_replies": True, "archive_reposts": True,
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
    assert dispatcher.calls == [("42", "manual", None)]
    assert source.calls == []


def test_account_task_filter_scopes_rows_pagination_and_counts(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), FakeTaskDispatcher())) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("42", "example")
        repository.upsert_account("142", "other42")
        lifecycle = TaskCenterRepository(settings.archive_db_path,
            settings.archive_sync_interval_seconds, settings.task_crawl_queue_name)
        for account_id, completed in [("42", False), ("42", False), ("42", True), ("142", False), (None, False)]:
            task_id = str(uuid.uuid4())
            labels = {TASK_OBSERVED_ACCOUNT_ID_LABEL: account_id} if account_id else {}
            lifecycle.record_queued(task_id, "archivex.sync_account", "queue-42", [], {}, labels)
            if completed:
                lifecycle.record_finished(task_id, labels, result={}, error=None,
                    name="archivex.sync_account", worker="queue-42")

        headers = {"Authorization": "Bearer test-token"}
        scope = {"observed_account_x_user_id": "42"}
        page = client.get("/api/task-center/tasks", params={**scope, "q": "queue-42", "limit": 1, "offset": 1}, headers=headers)
        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert len(page.json()["items"]) == 1
        assert page.json()["items"][0]["observed_account_x_user_id"] == "42"
        assert page.json()["counts"]["all"] == 3
        assert page.json()["counts"]["queued"] == 2
        completed = client.get("/api/task-center/tasks", params={**scope, "status": "completed"}, headers=headers).json()
        assert completed["total"] == 1
        assert completed["counts"]["queued"] == 2
        summary = client.get("/api/task-center/summary", params=scope, headers=headers).json()
        assert summary["counts"] == completed["counts"]
        assert client.get("/api/task-center/tasks", headers=headers).json()["total"] == 5
        assert client.get("/api/task-center/summary", headers=headers).json()["counts"]["all"] == 5
        empty = client.get("/api/task-center/tasks", params={"observed_account_x_user_id": "999"}, headers=headers).json()
        assert empty["items"] == []
        assert empty["counts"]["all"] == 0


def test_account_task_filter_scopes_current_failures(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, FakeSource(), FakeSessionAccountManager(), FakeTaskDispatcher())) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        repository.upsert_account("42", "example")
        repository.upsert_account("142", "other42")
        lifecycle = TaskCenterRepository(settings.archive_db_path,
            settings.archive_sync_interval_seconds, settings.task_crawl_queue_name)
        for account_id in ["42", "142"]:
            labels = {TASK_OBSERVED_ACCOUNT_ID_LABEL: account_id}
            lifecycle.record_finished(str(uuid.uuid4()), labels, result=None, error="network unavailable",
                name="archivex.sync_account", worker="queue-42", args=[account_id])
        headers = {"Authorization": "Bearer test-token"}
        parameters = {"observed_account_x_user_id": "42", "status": "failure"}
        failures = client.get("/api/task-center/tasks", params=parameters, headers=headers).json()
        assert failures["total"] == 1
        assert failures["items"][0]["observed_account_x_user_id"] == "42"
        assert failures["counts"]["failure"] == 1
        assert client.get("/api/task-center/summary", params={"observed_account_x_user_id": "42"}, headers=headers).json()["counts"]["failure"] == 1
        empty = client.get("/api/task-center/tasks", params={**parameters, "q": "missing"}, headers=headers).json()
        assert empty["total"] == 0
        assert empty["counts"]["failure"] == 1
        second_page = client.get("/api/task-center/tasks", params={**parameters, "offset": 1}, headers=headers).json()
        assert second_page["total"] == 1
        assert second_page["items"] == []
        assert client.get("/api/task-center/tasks", params={"status": "failure"}, headers=headers).json()["total"] == 2


def test_integrated_task_center_lists_and_reruns_tasks(tmp_path) -> None:
    settings = _settings(tmp_path)
    task_id = str(uuid.uuid4())
    initialize_storage(
        settings.archive_db_path,
        settings.archive_data_dir,
        settings.twscrape_session_path,
    )
    repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
    repository.upsert_account("42", "example", "Example")
    root_labels = {
        TASK_OBSERVED_ACCOUNT_ID_LABEL: "42",
        TASK_TRIGGER_LABEL: "manual",
    }
    lifecycle = TaskCenterRepository(
        settings.archive_db_path,
        settings.archive_sync_interval_seconds,
        settings.task_crawl_queue_name,
    )
    lifecycle.record_queued(
        task_id,
        "archivex.sync_account",
        "archivex:crawl",
        ["42", "manual"],
        {},
        root_labels,
        "2026-08-08T12:00:00Z",
    )
    lifecycle.record_started(
        task_id,
        "archivex.sync_account",
        "archivex:crawl",
        ["42", "manual"],
        {},
        root_labels,
        "2026-08-08T12:00:01Z",
    )
    lifecycle.record_finished(
        task_id,
        root_labels,
        result=None,
        error="network unavailable",
        finished_at="2026-08-08T12:00:03Z",
        name="archivex.sync_account",
        worker="archivex:crawl",
        args=["42", "manual"],
    )

    dispatcher = FakeTaskDispatcher()
    with TestClient(create_app(
        settings,
        FakeSource(),
        FakeSessionAccountManager(),
        dispatcher,
    )) as client:
        repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
        headers = {"Authorization": "Bearer test-token"}

        tasks = client.get("/api/task-center/tasks?status=failure", headers=headers)
        assert tasks.status_code == 200
        assert tasks.json()["total"] == 1
        assert tasks.json()["items"][0]["id"] == task_id
        assert tasks.json()["items"][0]["duration_ms"] == 2000
        assert tasks.json()["items"][0]["observed_account_x_user_id"] == "42"
        assert tasks.json()["items"][0]["trigger"] == "manual"
        assert tasks.json()["items"][0]["context"]["account"]["username"] == "example"

        summary = client.get("/api/task-center/summary", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["counts"] == {
            "all": 1,
            "queued": 0,
            "in_progress": 0,
            "retry_scheduled": 0,
            "completed": 0,
            "failure": 1,
            "abandoned": 0,
        }
        assert datetime.fromisoformat(summary.json()["generated_at"]).tzinfo is not None

        searched = client.get(f"/api/task-center/tasks?q={task_id}", headers=headers)
        assert searched.status_code == 200
        assert searched.json()["total"] == 1
        searched_account = client.get(
            "/api/task-center/tasks?q=example", headers=headers
        )
        assert searched_account.status_code == 200
        assert searched_account.json()["items"][0]["id"] == task_id

        detail = client.get(f"/api/task-center/tasks/{task_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "failure"

        rerun = client.post(
            f"/api/task-center/tasks/{task_id}/rerun", headers=headers
        )
        assert rerun.status_code == 202
        assert rerun.json()["task_id"] == "task-123"

        queued_task_id = uuid.uuid4()
        lifecycle.record_queued(
            str(queued_task_id),
            "archivex.sync_account",
            "archivex:crawl",
            ["999", "manual"],
            {},
            {},
            "2026-08-08T12:00:04Z",
        )
        blocked_rerun = client.post(
            f"/api/task-center/tasks/{queued_task_id}/rerun", headers=headers
        )
        assert blocked_rerun.status_code == 409

        abandoned_task_id = uuid.uuid4()
        lifecycle.record_publish_failed(
            str(abandoned_task_id),
            "archivex.sync_account",
            "archivex:crawl",
            ["42", "manual"],
            {},
            {},
            "publish failed",
        )
        invalid_clear = client.delete(
            "/api/task-center/tasks/history?status=queued",
            headers=headers,
        )
        assert invalid_clear.status_code == 422
        cleared = client.delete(
            "/api/task-center/tasks/history?status=abandoned",
            headers=headers,
        )
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
        linked_media_task_id = str(uuid.uuid4())
        linked_media_labels = {
            TASK_MEDIA_ID_LABEL: pending_media_id,
            TASK_PARENT_ID_LABEL: task_id,
        }
        lifecycle.record_queued(
            linked_media_task_id,
            "archivex.download_media",
            "archivex:media",
            [pending_media_id],
            {},
            linked_media_labels,
            "2026-08-08T12:30:00Z",
        )
        lifecycle.record_finished(
            linked_media_task_id,
            linked_media_labels,
            result={"status": "completed"},
            error=None,
            finished_at="2026-08-08T12:30:02Z",
            name="archivex.download_media",
            worker="archivex:media",
            args=[pending_media_id],
        )
        linked_detail = client.get(
            f"/api/task-center/tasks/{linked_media_task_id}", headers=headers
        )
        assert linked_detail.status_code == 200
        assert linked_detail.json()["parent_task_id"] == task_id
        assert linked_detail.json()["context"]["account"]["username"] == "example"
        assert linked_detail.json()["context"]["post"]["tweet_id"] == "100"
        assert linked_detail.json()["context"]["media"]["id"] == pending_media_id
        linked_rerun = client.post(
            f"/api/task-center/tasks/{linked_media_task_id}/rerun", headers=headers
        )
        assert linked_rerun.status_code == 202
        assert dispatcher.media_calls[0] == (
            pending_media_id,
            linked_media_task_id,
            task_id,
        )
        repository.upsert_account("85", "retrying")
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
            item_id = str(uuid.uuid4())
            task_labels = dict(labels)
            if name == "archivex.sync_account":
                task_labels.update({
                    TASK_OBSERVED_ACCOUNT_ID_LABEL: str(args[0]),
                    TASK_TRIGGER_LABEL: str(args[1]),
                })
            elif name == "archivex.download_media":
                task_labels[TASK_MEDIA_ID_LABEL] = str(args[0])
            lifecycle.record_queued(
                item_id, name, "archivex:crawl", args, {}, task_labels,
                started_at.replace(" ", "T") + "Z",
            )
            lifecycle.record_started(
                item_id, name, "archivex:crawl", args, {}, task_labels,
                started_at.replace(" ", "T") + "Z",
            )
            lifecycle.record_finished(
                item_id,
                task_labels,
                result=None,
                error="failed" if task_status == 2 else None,
                finished_at=started_at.replace(" ", "T") + "Z",
                name=name,
                worker="archivex:crawl",
                args=args,
                kwargs={},
            )

        current_failures = client.get(
            "/api/task-center/tasks?status=failure", headers=headers
        )
        assert current_failures.status_code == 200
        assert current_failures.json()["total"] == 5
        assert current_failures.json()["counts"]["failure"] == 5
        assert sorted(task["name"] for task in current_failures.json()["items"]) == [
            "archivex.download_media",
            "archivex.schedule_enabled_accounts",
            "archivex.sync_account",
            "archivex.sync_account",
            "archivex.unknown_task",
        ]

        retry_failures = client.post(
            "/api/task-center/failures/retry", headers=headers
        )
        assert retry_failures.status_code == 202
        assert retry_failures.json() == {
            "queued": 5,
            "duplicates": 0,
            "skipped_resolved": 2,
            "automatic_retrying": 0,
            "unsupported": 1,
            "failed": 0,
        }

    assert dispatcher.calls[0] == ("42", "rerun", str(task_id))
    assert dispatcher.calls[1:4] == [
        ("42", "schedule_manual", None),
        ("42", "schedule_manual", None),
        ("85", "schedule_manual", None),
    ]
    assert dispatcher.calls[4][0:2] == ("85", "failure_retry")
    assert dispatcher.calls[4][2] is not None
    assert dispatcher.calls[5][0:2] == ("42", "failure_retry")
    assert dispatcher.calls[5][2] is not None
    assert dispatcher.media_calls[1][0] == pending_media_id
    assert dispatcher.media_calls[1][1] is not None
    assert dispatcher.media_calls[1][2] is None
