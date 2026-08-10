import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "start_backend.py"
SPEC = importlib.util.spec_from_file_location("start_backend", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
start_backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = start_backend
SPEC.loader.exec_module(start_backend)


def test_process_specs_start_backend_only(tmp_path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    specs = start_backend.build_process_specs(tmp_path, venv_bin)
    by_name = {spec.name: spec for spec in specs}

    assert list(by_name) == ["api", "crawl-worker", "media-worker", "scheduler"]
    assert by_name["api"].command == (str(venv_bin / "archivex"),)
    assert by_name["crawl-worker"].env == {
        "TASK_WORKER_QUEUE_NAME": "archivex:crawl"
    }
    assert by_name["media-worker"].env == {
        "TASK_WORKER_QUEUE_NAME": "archivex:media"
    }
    assert by_name["scheduler"].env == {
        "TASK_WORKER_QUEUE_NAME": "archivex:crawl"
    }


def test_validate_project_reports_setup_when_dependencies_are_missing(tmp_path) -> None:
    try:
        start_backend.validate_project(tmp_path)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("validate_project should reject an incomplete checkout")

    assert ".env" in message
    assert ".venv/bin/archivex" in message
    assert "scripts/setup_venv.py" in message


def test_validate_project_does_not_require_frontend_dependencies(tmp_path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (tmp_path / ".env").touch()
    for command in ("python", "archivex", "taskiq"):
        (venv_bin / command).touch()

    assert start_backend.validate_project(tmp_path) == venv_bin


def test_start_process_applies_only_its_environment_overrides(monkeypatch, tmp_path) -> None:
    invocation = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(command, **kwargs):
        invocation.update(command=command, **kwargs)
        return FakeProcess()

    monkeypatch.setattr(start_backend.subprocess, "Popen", fake_popen)
    spec = start_backend.ProcessSpec(
        "media-worker",
        ("taskiq", "worker"),
        tmp_path,
        {"TASK_WORKER_QUEUE_NAME": "archivex:media"},
    )

    start_backend.start_process(spec, {"PATH": "/bin", "KEEP": "yes"})

    assert invocation["command"] == spec.command
    assert invocation["cwd"] == tmp_path
    assert invocation["env"] == {
        "PATH": "/bin",
        "KEEP": "yes",
        "TASK_WORKER_QUEUE_NAME": "archivex:media",
    }


def test_existing_queue_processes_are_discovered_for_this_project(tmp_path, monkeypatch) -> None:
    taskiq = tmp_path / ".venv" / "bin" / "taskiq"

    def fake_run(command, **kwargs):
        assert command == ["ps", "-axo", "pid=,command="]
        return type("Result", (), {
            "stdout": (
                f"  101 {tmp_path}/.venv/bin/python {taskiq} worker "
                "archivex.tasks:broker --workers 1\n"
                f"  102 {tmp_path}/.venv/bin/python {taskiq} scheduler "
                "archivex.tasks:scheduler\n"
                "  103 /other/project/.venv/bin/taskiq worker archivex.tasks:broker\n"
            ),
        })()

    monkeypatch.setattr(start_backend.subprocess, "run", fake_run)

    existing = start_backend.find_existing_queue_processes(tmp_path)

    assert [item.pid for item in existing] == [101, 102]
