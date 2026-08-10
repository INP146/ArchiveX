import importlib.util
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SCRIPT_PATH = SCRIPT_DIR / "dev.py"
SPEC = importlib.util.spec_from_file_location("dev", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
dev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dev
SPEC.loader.exec_module(dev)


def test_process_specs_start_complete_local_stack(tmp_path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    specs = dev.build_process_specs(tmp_path, venv_bin, "/usr/local/bin/npm")
    by_name = {spec.name: spec for spec in specs}

    assert list(by_name) == ["api", "crawl-worker", "media-worker", "scheduler", "web"]
    assert by_name["web"].command == (
        "/usr/local/bin/npm",
        "run",
        "dev",
        "--",
        "--host",
        "--strictPort",
    )
    assert by_name["web"].cwd == tmp_path / "frontend"


def test_validate_frontend_reports_setup_when_dependencies_are_missing(tmp_path) -> None:
    try:
        dev.validate_frontend(tmp_path)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("validate_frontend should reject an incomplete checkout")

    assert "frontend/package.json" in message
    assert "frontend/node_modules" in message
    assert "scripts/setup_venv.py" in message
