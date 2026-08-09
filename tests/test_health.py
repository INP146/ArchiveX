import sqlite3

from fastapi.testclient import TestClient

from archivex.config import Settings
from archivex.main import create_app
from archivex.storage import ArchiveRepository, initialize_storage


def test_health_initializes_persistent_storage(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path=tmp_path / "archive.sqlite3",
        archive_data_dir=tmp_path / "archive",
        twscrape_session_path=tmp_path / "sessions",
        web_auth_token="test-token",
        task_queue_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
        readiness = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert readiness.json()["checks"]["queue"]["status"] == "disabled"
    assert settings.archive_db_path.exists()
    assert settings.archive_data_dir.is_dir()
    assert settings.twscrape_session_path.is_dir()
    with sqlite3.connect(settings.archive_db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"accounts", "posts", "media", "queue_tasks", "queue_attempts"} <= tables


def test_api_startup_does_not_change_worker_owned_sync_runs(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path=tmp_path / "archive.sqlite3",
        archive_data_dir=tmp_path / "archive",
        twscrape_session_path=tmp_path / "sessions",
        web_auth_token="test-token",
        task_queue_enabled=False,
    )
    initialize_storage(
        settings.archive_db_path, settings.archive_data_dir, settings.twscrape_session_path
    )
    repository = ArchiveRepository(settings.archive_db_path, settings.archive_data_dir)
    account = repository.upsert_account("42", "example")
    repository.set_account_enabled(account.x_user_id, False)
    run_id = repository.start_sync_run(account.x_user_id)

    with TestClient(create_app(settings)):
        run = next(item for item in repository.list_sync_runs() if item.id == run_id)

    assert run.status == "running"
    assert run.finished_at is None


def test_api_liveness_starts_without_redis(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path=tmp_path / "archive.sqlite3",
        archive_data_dir=tmp_path / "archive",
        twscrape_session_path=tmp_path / "sessions",
        task_redis_url="redis://127.0.0.1:1/0",
        web_auth_token="test-token",
        task_queue_enabled=True,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert settings.archive_db_path.is_file()
