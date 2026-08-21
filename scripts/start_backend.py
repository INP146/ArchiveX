#!/usr/bin/env python3
"""Start the ArchiveX backend development processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingProcess:
    pid: int
    command: str


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def validate_project(root: Path) -> Path:
    venv_bin = root / ".venv" / "bin"
    required_paths = (
        root / ".env",
        venv_bin / "python",
        venv_bin / "archivex",
        venv_bin / "taskiq",
    )
    missing = [path for path in required_paths if not path.exists()]
    if missing:
        formatted = "\n".join(f"  - {path.relative_to(root)}" for path in missing)
        raise RuntimeError(
            f"Local development environment is incomplete:\n{formatted}\n"
            "Run scripts/setup_venv.py first."
        )

    return venv_bin


def find_existing_queue_processes(root: Path) -> list[ExistingProcess]:
    if os.name != "posix":
        return []
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    taskiq = str(root / ".venv" / "bin" / "taskiq")
    markers = (
        f"{taskiq} worker archivex.tasks:broker",
        f"{taskiq} scheduler archivex.tasks:scheduler",
    )
    existing: list[ExistingProcess] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not any(marker in fields[1] for marker in markers):
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != os.getpid():
            existing.append(ExistingProcess(pid, fields[1]))
    return existing


def build_process_specs(root: Path, venv_bin: Path) -> tuple[ProcessSpec, ...]:
    taskiq = str(venv_bin / "taskiq")
    worker_base = (
        "worker",
        "archivex.tasks:broker",
        "--workers",
        "1",
        "--ack-type",
        "when_saved",
    )
    return (
        ProcessSpec("api", (str(venv_bin / "archivex"),), root),
        ProcessSpec(
            "crawl-worker",
            (
                taskiq,
                *worker_base,
                "--max-async-tasks",
                "1",
                "--max-prefetch",
                "1",
                "--shutdown-timeout",
                "30",
            ),
            root,
            {"TASK_WORKER_QUEUE_NAME": "archivex:crawl"},
        ),
        ProcessSpec(
            "media-worker",
            (
                taskiq,
                *worker_base,
                "--max-async-tasks",
                "4",
                "--max-prefetch",
                "4",
                "--shutdown-timeout",
                "30",
            ),
            root,
            {"TASK_WORKER_QUEUE_NAME": "archivex:media"},
        ),
        ProcessSpec(
            "scheduler",
            (taskiq, "scheduler", "archivex.tasks:scheduler"),
            root,
            {"TASK_WORKER_QUEUE_NAME": "archivex:crawl"},
        ),
    )


def read_runtime_settings(root: Path, python: Path, env: dict[str, str]) -> dict[str, object]:
    code = (
        "import json; "
        "from archivex.config import get_settings; "
        "s = get_settings(); "
        "print(json.dumps({'web_port': s.web_port}))"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def check_redis(root: Path, python: Path, env: dict[str, str]) -> None:
    code = (
        "from redis import Redis; "
        "from archivex.config import get_settings; "
        "client = Redis.from_url(get_settings().task_redis_url, "
        "socket_connect_timeout=2, socket_timeout=2); "
        "assert client.ping(); client.close()"
    )
    subprocess.run(
        [str(python), "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def start_process(spec: ProcessSpec, base_env: dict[str, str]) -> subprocess.Popen:
    env = {**base_env, **spec.env}
    process = subprocess.Popen(
        spec.command,
        cwd=spec.cwd,
        env=env,
        start_new_session=os.name == "posix",
    )
    print(f"Started {spec.name} (PID {process.pid})", flush=True)
    return process


def wait_for_api(
    process: subprocess.Popen,
    port: int,
    should_stop: Callable[[], bool],
    timeout_seconds: float = 30,
) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not should_stop():
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    return False


def signal_process(process: subprocess.Popen, signum: int) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
    except ProcessLookupError:
        pass


def stop_processes(processes: list[tuple[ProcessSpec, subprocess.Popen]]) -> None:
    for _, process in reversed(processes):
        signal_process(process, signal.SIGTERM)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if all(process.poll() is not None for _, process in processes):
            return
        time.sleep(0.1)

    for spec, process in reversed(processes):
        if process.poll() is None:
            print(f"Force-stopping {spec.name} (PID {process.pid})", file=sys.stderr)
            signal_process(process, signal.SIGKILL)


def run_process_specs(
    root: Path,
    venv_bin: Path,
    specs: tuple[ProcessSpec, ...],
    *,
    ready_url: str | None = None,
    stack_label: str = "backend",
) -> int:
    try:
        existing = find_existing_queue_processes(root)
    except subprocess.SubprocessError as exc:
        print(f"Could not inspect existing queue processes: {exc}", file=sys.stderr)
        return 1
    if existing:
        details = "\n".join(f"  - PID {item.pid}: {item.command}" for item in existing)
        print(
            "ArchiveX queue processes are already running; refusing to start a duplicate stack:\n"
            f"{details}\nStop the existing stack cleanly before starting another one.",
            file=sys.stderr,
        )
        return 1

    base_env = os.environ.copy()
    base_env["PATH"] = os.pathsep.join((str(venv_bin), base_env.get("PATH", "")))
    base_env["PYTHONUNBUFFERED"] = "1"
    base_env["TASK_QUEUE_ENABLED"] = "true"
    base_env["TASK_WORKER_QUEUE_NAME"] = "archivex:crawl"

    try:
        runtime = read_runtime_settings(root, venv_bin / "python", base_env)
        check_redis(root, venv_bin / "python", base_env)
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"Local configuration or Redis check failed: {detail.strip()}", file=sys.stderr)
        print("Start the host Redis configured by TASK_REDIS_URL, then try again.", file=sys.stderr)
        return 1

    processes: list[tuple[ProcessSpec, subprocess.Popen]] = []
    requested_signal: int | None = None

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal requested_signal
        requested_signal = requested_signal or signum

    previous_handlers = {
        signum: signal.signal(signum, request_shutdown)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    exit_code = 0
    try:
        api_spec = specs[0]
        api_process = start_process(api_spec, base_env)
        processes.append((api_spec, api_process))
        if not wait_for_api(
            api_process,
            int(runtime["web_port"]),
            lambda: requested_signal is not None,
        ):
            if requested_signal is None:
                print("API did not become healthy within 30 seconds.", file=sys.stderr)
                exit_code = api_process.returncode or 1
        else:
            for spec in specs[1:]:
                process = start_process(spec, base_env)
                processes.append((spec, process))

            resolved_ready_url = ready_url or f"http://localhost:{runtime['web_port']}"
            print(f"ArchiveX {stack_label} is ready at {resolved_ready_url}", flush=True)
            while requested_signal is None:
                stopped = next(
                    (
                        (spec, process)
                        for spec, process in processes
                        if process.poll() is not None
                    ),
                    None,
                )
                if stopped is not None:
                    spec, process = stopped
                    print(
                        f"{spec.name} exited unexpectedly with code {process.returncode}.",
                        file=sys.stderr,
                    )
                    exit_code = process.returncode or 1
                    break
                time.sleep(0.25)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Could not start the ArchiveX {stack_label}: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if processes:
            print(f"Stopping ArchiveX {stack_label} processes...", flush=True)
            stop_processes(processes)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if requested_signal is not None:
        return 128 + requested_signal
    return exit_code


def main() -> int:
    root = project_root()
    try:
        venv_bin = validate_project(root)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    specs = build_process_specs(root, venv_bin)
    return run_process_specs(root, venv_bin, specs)


if __name__ == "__main__":
    raise SystemExit(main())
