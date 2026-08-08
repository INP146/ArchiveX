import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "setup_venv.py"
SPEC = importlib.util.spec_from_file_location("setup_venv", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
setup_venv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_venv)


def test_initialize_env_copies_example_without_overwriting(tmp_path) -> None:
    example = tmp_path / ".env.example"
    example.write_text("WEB_PORT=8000\n")

    setup_venv.initialize_env(tmp_path)
    assert (tmp_path / ".env").read_text() == "WEB_PORT=8000\n"

    (tmp_path / ".env").write_text("WEB_PORT=8002\n")
    setup_venv.initialize_env(tmp_path)
    assert (tmp_path / ".env").read_text() == "WEB_PORT=8002\n"


def test_main_updates_existing_python_and_frontend_dependencies(tmp_path, monkeypatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").touch()
    (tmp_path / ".env").touch()
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    monkeypatch.setattr(setup_venv, "__file__", str(scripts / "setup_venv.py"))
    monkeypatch.setattr(
        setup_venv,
        "require_tool",
        lambda name: f"/tools/{name}",
    )
    invocations = []

    def fake_run(command, **kwargs):
        invocations.append((command, kwargs))

    monkeypatch.setattr(setup_venv.subprocess, "run", fake_run)

    assert setup_venv.main() == 0
    assert invocations == [
        (
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            {"check": True},
        ),
        (
            [str(venv_python), "-m", "pip", "install", "-e", ".[dev]"],
            {"cwd": tmp_path, "check": True},
        ),
        (
            ["/tools/npm", "ci"],
            {"cwd": frontend, "check": True},
        ),
    ]
