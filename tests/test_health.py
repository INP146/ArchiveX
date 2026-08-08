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

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert settings.archive_db_path.exists()
    assert settings.archive_data_dir.is_dir()
    assert settings.twscrape_session_path.is_dir()


def test_startup_closes_sync_runs_left_running_by_previous_process(tmp_path) -> None:
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

    assert run.status == "interrupted"
    assert run.finished_at is not None
