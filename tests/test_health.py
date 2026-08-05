from fastapi.testclient import TestClient

from archivex.config import Settings
from archivex.main import create_app


def test_health_initializes_persistent_storage(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        archive_db_path=tmp_path / "archive.sqlite3",
        archive_data_dir=tmp_path / "archive",
        twscrape_session_path=tmp_path / "sessions",
        web_auth_token="test-token",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert settings.archive_db_path.exists()
    assert settings.archive_data_dir.is_dir()
    assert settings.twscrape_session_path.is_dir()
