import importlib.util
import json
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def clean_release_environment(monkeypatch):
    for key in release.release_ai.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def release_root(tmp_path, clean_release_environment):
    for filename in (
        "pyproject.toml",
        "frontend/package.json",
        "frontend/package-lock.json",
        "docker-compose.ghcr.yml",
        "README.md",
    ):
        destination = tmp_path / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / filename, destination)
    return tmp_path


def next_rc(root):
    major, minor, patch, _ = release.version_key(release.current_version(root))
    return f"{major}.{minor}.{patch + 1}-rc1"


def snapshot(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def complete_notes(root, version):
    path = release.notes_path(root, version)
    path.write_text(
        f"# ArchiveX v{version} 发布说明\n\n"
        "## 主要变更\n\n修复归档问题。\n\n"
        "## 升级\n\n本版本不改变数据库 schema，无需迁移。\n"
    )


def test_prepare_syncs_metadata_preserving_history_and_dependencies(release_root):
    version = next_rc(release_root)
    readme_before = (release_root / "README.md").read_text()
    lock_before = json.loads((release_root / "frontend/package-lock.json").read_text())

    release.prepare(release_root, f"v{version}")
    release.check(release_root, f"v{version}")

    assert release.current_version(release_root) == version
    lock_after = json.loads((release_root / "frontend/package-lock.json").read_text())
    lock_before["version"] = version
    lock_before["packages"][""]["version"] = version
    assert lock_after == lock_before
    assert release.readme_section(readme_before)[::2] == release.readme_section(
        (release_root / "README.md").read_text()
    )[::2]
    assert release.TODO in release.notes_path(release_root, version).read_text()


def test_prepare_dry_run_changes_nothing(release_root):
    before = snapshot(release_root)
    paths = release.prepare(release_root, next_rc(release_root), dry_run=True)
    assert len(paths) == 6
    assert snapshot(release_root) == before


def test_existing_notes_are_never_overwritten(release_root):
    version = next_rc(release_root)
    path = release.notes_path(release_root, version)
    path.parent.mkdir()
    path.write_text("手工写好的升级说明\n")
    before = snapshot(release_root)
    with pytest.raises(ValueError, match="已存在"):
        release.prepare(release_root, version)
    assert snapshot(release_root) == before


def test_same_version_and_downgrade_are_rejected_without_writes(release_root):
    before = snapshot(release_root)
    for version in (release.current_version(release_root), "0.0.0"):
        with pytest.raises(ValueError, match="必须高于"):
            release.prepare(release_root, version)
    assert snapshot(release_root) == before


@pytest.mark.parametrize("version", ["1.2", "1.2.3-beta1", "1.2.3-rc0", "01.2.3", "1.2.3-rc01", "1.2.3+build", "1.2.3\n"])
def test_invalid_versions_are_rejected(version):
    with pytest.raises(ValueError, match="版本格式"):
        release.normalize_version(version)


def test_stable_release_after_rc_has_pep440_version(release_root):
    previous = release.current_version(release_root)
    stable = previous.split("-")[0]
    if "-rc" not in previous:
        major, minor, patch, _ = release.version_key(previous)
        stable = f"{major}.{minor}.{patch + 1}"
    release.prepare(release_root, stable)
    release.check(release_root, stable)
    assert release.PROJECT_VERSION_RE.search((release_root / "pyproject.toml").read_text())[2] == stable


@pytest.mark.parametrize("filename", ["pyproject.toml", "frontend/package.json", "frontend/package-lock.json", "docker-compose.ghcr.yml", "README.md"])
def test_mismatched_version_blocks_release_and_preparation(release_root, filename):
    version = release.current_version(release_root)
    path = release_root / filename
    bad = release.python_version(version) if filename == "pyproject.toml" else version
    path.write_text(path.read_text().replace(bad, "9.9.9", 1))
    before = snapshot(release_root)
    with pytest.raises(ValueError):
        release.check(release_root, version)
    with pytest.raises(ValueError):
        release.prepare(release_root, next_rc(release_root))
    assert snapshot(release_root) == before


def test_lockfile_nested_version_must_match(release_root):
    path = release_root / "frontend/package-lock.json"
    lock = json.loads(path.read_text())
    lock["packages"][""]["version"] = "9.9.9"
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="根包版本"):
        release.check(release_root, release.current_version(release_root))


def test_tag_must_match_checkout(release_root):
    with pytest.raises(ValueError):
        release.check(release_root, "v9.9.9")


def test_missing_and_unfinished_notes_block_publication(release_root):
    version = next_rc(release_root)
    with pytest.raises(ValueError, match="缺少发布说明"):
        release.check(release_root, release.current_version(release_root), require_notes=True)
    release.prepare(release_root, version)
    with pytest.raises(ValueError, match="草稿"):
        release.check(release_root, version, require_notes=True)
    complete_notes(release_root, version)
    release.check(release_root, version, require_notes=True)


def test_notes_must_have_matching_title_and_upgrade_section(release_root):
    version = next_rc(release_root)
    release.prepare(release_root, version)
    complete_notes(release_root, version)
    path = release.notes_path(release_root, version)
    path.write_text(path.read_text().replace(version, "9.9.9").replace("## 升级", "## 其他"))
    with pytest.raises(ValueError, match="标题"):
        release.check(release_root, version, require_notes=True)


def test_removing_placeholders_without_writing_upgrade_notes_still_blocks_release(release_root):
    version = next_rc(release_root)
    release.prepare(release_root, version)
    release.notes_path(release_root, version).write_text(
        f"# ArchiveX v{version} 发布说明\n\n## 主要变更\n\n修复归档问题。\n\n## 升级\n\n"
    )
    with pytest.raises(ValueError, match="未填写 ## 升级"):
        release.check(release_root, version, require_notes=True)


def test_cli_reports_failed_check_without_traceback(release_root, monkeypatch, capsys):
    monkeypatch.setattr(release, "ROOT", release_root)
    assert release.main(["check", "v9.9.9"]) == 1
    assert "发布准备/校验失败" in capsys.readouterr().err


def test_prepare_reads_env_and_keeps_ai_notes_blocked_until_review(release_root, monkeypatch):
    (release_root / ".env").write_text("RELEASE_AI_PROVIDER=claude\nRELEASE_AI_MODEL=custom-model\n")
    calls = []

    def generate(root, version, previous, config, *, template, base_tag=None):
        calls.append((root, version, previous, config))
        return template + "\n修复归档恢复。本版本无需数据库迁移；升级前备份。\n"

    monkeypatch.setattr(release.release_ai, "generate", generate)
    previous = release.current_version(release_root)
    version = next_rc(release_root)
    release.prepare(release_root, version)
    assert calls[0][3].provider == "claude"
    assert calls[0][3].model == "custom-model"
    notes = release.notes_path(release_root, version).read_text()
    assert "修复归档恢复" in notes
    assert f"v{previous} 到当前工作区" in notes
    with pytest.raises(ValueError, match="草稿"):
        release.check(release_root, version, require_notes=True)


def test_ai_failure_leaves_all_files_unchanged(release_root, monkeypatch):
    (release_root / ".env").write_text("RELEASE_AI_PROVIDER=codex\n")
    before = snapshot(release_root)

    def fail(*args, **kwargs):
        raise ValueError("CLI 登录失败")

    monkeypatch.setattr(release.release_ai, "generate", fail)
    with pytest.raises(ValueError, match="登录失败"):
        release.prepare(release_root, next_rc(release_root))
    assert snapshot(release_root) == before


def test_ai_dry_run_never_invokes_model(release_root, monkeypatch):
    (release_root / ".env").write_text("RELEASE_AI_PROVIDER=codex\n")
    before = snapshot(release_root)
    monkeypatch.setattr(release.release_ai, "validate_base_tag", lambda *args: "v0.1.2-rc3")

    def unexpected(*args):
        pytest.fail("dry-run called the model")

    monkeypatch.setattr(release.release_ai, "generate", unexpected)
    release.prepare(release_root, next_rc(release_root), dry_run=True)
    assert snapshot(release_root) == before


def test_cli_base_tag_is_forwarded_to_ai_and_saved_in_draft(release_root, monkeypatch):
    (release_root / ".env").write_text("RELEASE_AI_PROVIDER=codex\n")
    monkeypatch.setattr(release, "ROOT", release_root)
    calls = []

    def generate(root, version, previous, config, *, template, base_tag=None):
        calls.append(base_tag)
        return template + "\n修复归档问题。升级要求待人工确认。\n"

    monkeypatch.setattr(release.release_ai, "generate", generate)
    version = next_rc(release_root)
    assert release.main(["prepare", version, "--base-tag", "v0.1.1"]) == 0
    assert calls == ["v0.1.1"]
    assert "v0.1.1 到当前工作区" in release.notes_path(release_root, version).read_text()


def test_invalid_base_tag_does_not_change_files(release_root):
    before = snapshot(release_root)
    with pytest.raises(ValueError, match="--base-tag"):
        release.prepare(release_root, next_rc(release_root), base_tag="not-a-tag")
    assert snapshot(release_root) == before
