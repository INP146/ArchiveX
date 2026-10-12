import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("release_ai", Path(__file__).parents[1] / "scripts/release_ai.py")
assert SPEC is not None and SPEC.loader is not None
release_ai = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_ai)


@pytest.fixture(autouse=True)
def clean_release_environment(monkeypatch):
    for key in release_ai.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_dotenv_quotes_comments_and_environment_precedence(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    text = (
        'WEB_AUTH_TOKEN="local-secret-with-$()"\n'
        'export RELEASE_AI_PROVIDER="claude" # chosen CLI\n'
        "RELEASE_AI_MODEL='literal-$(model)'\n"
        "RELEASE_AI_TIMEOUT_SECONDS=42\n"
    )
    path.write_text(text)
    config = release_ai.load_config(tmp_path)
    assert config == release_ai.AIConfig("claude", "literal-$(model)", 42)
    monkeypatch.setenv("RELEASE_AI_PROVIDER", "codex")
    assert release_ai.load_config(tmp_path).provider == "codex"
    assert path.read_text() == text


@pytest.mark.parametrize("line", ["RELEASE_AI_PROVIDER=unknown", "RELEASE_AI_TIMEOUT_SECONDS=0", "RELEASE_AI_TIMEOUT_SECONDS=no", 'RELEASE_AI_MODEL="unclosed'])
def test_invalid_config_fails_explicitly(tmp_path, line):
    (tmp_path / ".env").write_text(line)
    with pytest.raises(ValueError):
        release_ai.load_config(tmp_path)


def test_missing_env_uses_template_mode(tmp_path):
    assert release_ai.load_config(tmp_path) == release_ai.AIConfig("none", "", 600)


@pytest.fixture
def git_root(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/change.py").write_text("before = True\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    git("tag", "v0.1.2-rc3")
    (tmp_path / "src/change.py").write_text("after = True\n")
    git("add", ".")
    git("commit", "-qm", "fix archive recovery")
    (tmp_path / "src/change.py").write_text("after = True\nworking_tree = True\n")
    (tmp_path / "src/new.py").write_text("new_source = True\n")
    (tmp_path / ".env").write_text("WEB_AUTH_TOKEN=secret-do-not-send\n")
    (tmp_path / "private.txt").write_text("private-do-not-send\n")
    (tmp_path / ".gitignore").write_text(".env\n")
    return tmp_path


def test_missing_base_tag_stops_before_generation(git_root):
    with pytest.raises(ValueError, match="tag 存在"):
        release_ai.validate_base_tag(git_root, "9.9.9")


def test_explicit_base_tag_overrides_default_version_tag(git_root):
    assert release_ai.validate_base_tag(git_root, "9.9.9", "v0.1.2-rc3") == "v0.1.2-rc3"


def test_obsolete_environment_setting_does_not_control_base(git_root, monkeypatch):
    monkeypatch.setenv("RELEASE_AI_BASE_TAG", "v9.9.9")
    with (git_root / ".env").open("a") as env:
        env.write("RELEASE_AI_BASE_TAG=v8.8.8\n")
    assert release_ai.load_config(git_root) == release_ai.AIConfig("none", "", 600)
    assert release_ai.validate_base_tag(git_root, "0.1.2-rc3") == "v0.1.2-rc3"


@pytest.fixture
def template():
    return (
        "# ArchiveX v0.1.2-rc4 发布说明\n\nRELEASE_TODO：人工审阅。\n\n"
        "## 主要变更\n\nRELEASE_TODO：填写变更。\n\n"
        "## 镜像\n\nghcr.io/inp146/archivex:0.1.2-rc4\n\n"
        "## 升级\n\nRELEASE_TODO：填写升级要求。\n\n"
        "## 发布验证\n\nCI 执行验证。\n"
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_agent_runs_in_repository_and_directly_writes_release_notes(git_root, monkeypatch, provider, template):
    monkeypatch.setattr(release_ai.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(release_ai, "validate_base_tag", lambda *args: "v0.1.2-rc3")
    calls = []
    path = git_root / ".agents/RELEASE_V0.1.2_RC4.md"
    content = template.replace("RELEASE_TODO：填写变更。", "- 修复归档恢复。")
    content = content.replace("RELEASE_TODO：填写升级要求。", "本次无需数据库迁移。")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert path.read_text() == template
        assert (kwargs["cwd"] / "src/change.py").is_file()
        path.write_text(content)
        stdout = "Finished writing the file" if provider == "codex" else json.dumps({"is_error": False})
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(release_ai.subprocess, "run", run)
    result = release_ai.generate(git_root, "0.1.2-rc4", "0.1.2-rc3", release_ai.AIConfig(provider, "custom-model", 42), template=template)
    assert result == content == path.read_text()
    command, kwargs = calls[0]
    assert kwargs["timeout"] == 42
    assert kwargs["cwd"] == git_root
    assert ".agents/RELEASE_V0.1.2_RC4.md" in kwargs["input"]
    assert "v0.1.2-rc3" in kwargs["input"]
    assert "custom-model" in command
    if provider == "codex":
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        assert command[command.index("--cd") + 1] == str(git_root)
    else:
        assert "Read" in command and "Write" in command and "Bash(git:*)" in command


@pytest.mark.parametrize("bad_content", [None, "wrong title\n", "# ArchiveX v0.1.2-rc4 发布说明\n\n## 主要变更\n"])
def test_missing_or_malformed_agent_document_is_rejected_and_removed(git_root, monkeypatch, bad_content, template):
    monkeypatch.setattr(release_ai.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(release_ai, "validate_base_tag", lambda *args: "v0.1.2-rc3")
    path = git_root / ".agents/RELEASE_V0.1.2_RC4.md"

    def run(command, **kwargs):
        if bad_content is not None:
            path.write_text(bad_content)
        return subprocess.CompletedProcess(command, 0, "done", "")

    monkeypatch.setattr(release_ai.subprocess, "run", run)
    with pytest.raises(ValueError):
        release_ai.generate(git_root, "0.1.2-rc4", "0.1.2-rc3", release_ai.AIConfig("codex", "", 60), template=template)
    assert not path.exists()


def test_timeout_is_reported_and_partial_draft_removed(git_root, monkeypatch, template):
    monkeypatch.setattr(release_ai.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(release_ai, "validate_base_tag", lambda *args: "v0.1.2-rc3")
    path = git_root / ".agents/RELEASE_V0.1.2_RC4.md"

    def timeout(command, **kwargs):
        path.write_text("partial result")
        raise subprocess.TimeoutExpired(command, 42, output="sensitive provider output")

    monkeypatch.setattr(release_ai.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="生成超时") as exc:
        release_ai.generate(git_root, "0.1.2-rc4", "0.1.2-rc3", release_ai.AIConfig("codex", "", 42), template=template)
    assert "sensitive" not in str(exc.value)
    assert not path.exists()


def test_existing_release_notes_are_preserved(git_root, monkeypatch, template):
    monkeypatch.setattr(release_ai.shutil, "which", lambda name: "/bin/codex")
    path = git_root / ".agents/RELEASE_V0.1.2_RC4.md"
    path.parent.mkdir()
    path.write_text("existing human notes")
    with pytest.raises(ValueError, match="已存在"):
        release_ai.generate(git_root, "0.1.2-rc4", "0.1.2-rc3", release_ai.AIConfig("codex", "", 42), template=template)
    assert path.read_text() == "existing human notes"


def test_review_marker_is_restored_if_agent_removes_it(git_root, monkeypatch, template):
    monkeypatch.setattr(release_ai.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(release_ai, "validate_base_tag", lambda *args: "v0.1.2-rc3")
    path = git_root / ".agents/RELEASE_V0.1.2_RC4.md"

    def run(command, **kwargs):
        path.write_text(template.replace("RELEASE_TODO", "已填写"))
        return subprocess.CompletedProcess(command, 0, "done", "")

    monkeypatch.setattr(release_ai.subprocess, "run", run)
    result = release_ai.generate(git_root, "0.1.2-rc4", "0.1.2-rc3", release_ai.AIConfig("codex", "", 42), template=template)
    assert "RELEASE_TODO" in result == path.read_text()
