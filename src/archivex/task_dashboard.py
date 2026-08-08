from __future__ import annotations

from taskiq_dashboard import TaskiqDashboard

from archivex.config import Settings


def create_task_dashboard(settings: Settings) -> TaskiqDashboard:
    from archivex.tasks import broker, dashboard_api_token, scheduler

    settings.task_dashboard_db_path.parent.mkdir(parents=True, exist_ok=True)
    database_dsn = f"sqlite+aiosqlite:///{settings.task_dashboard_db_path.as_posix()}"
    return TaskiqDashboard(
        api_token=dashboard_api_token(settings),
        storage_type="sqlite",
        database_dsn=database_dsn,
        broker=broker,
        scheduler=scheduler,
        root_path=settings.task_dashboard_path,
    )
