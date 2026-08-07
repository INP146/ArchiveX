import importlib.util
import os
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "start_backend.py"
SPEC = importlib.util.spec_from_file_location("start_backend", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
start_backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start_backend)


def test_start_backend_adds_virtualenv_bin_to_path(tmp_path, monkeypatch) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    archivex_cmd = tmp_path / ".venv" / "bin" / "archivex"
    archivex_cmd.parent.mkdir(parents=True)
    archivex_cmd.touch()
    monkeypatch.setattr(start_backend, "__file__", str(scripts_dir / "start_backend.py"))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

    invocation = {}

    def capture_chdir(cwd):
        invocation["cwd"] = cwd

    def capture_execve(executable, arguments, env):
        invocation.update(executable=executable, arguments=arguments, env=env)

    monkeypatch.setattr(start_backend.os, "chdir", capture_chdir)
    monkeypatch.setattr(start_backend.os, "execve", capture_execve)

    start_backend.main()

    assert invocation["executable"] == archivex_cmd
    assert invocation["arguments"] == [str(archivex_cmd)]
    assert invocation["cwd"] == tmp_path
    assert invocation["env"]["PATH"].split(os.pathsep) == [
        str(archivex_cmd.parent),
        "/usr/local/bin",
        "/usr/bin",
    ]
