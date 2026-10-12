"""Generate release-note drafts with a locally configured AI CLI (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple


ENV_KEYS = (
    "RELEASE_AI_PROVIDER",
    "RELEASE_AI_MODEL",
    "RELEASE_AI_TIMEOUT_SECONDS",
)


class AIConfig(NamedTuple):
    provider: str
    model: str
    timeout: int


def load_config(root: Path) -> AIConfig:
    """Read only release settings; never source .env or export app credentials."""
    values = {}
    env_path = root / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:export\s+)?(\w+)\s*=\s*(.*?)\s*$", line)
            if not match or match[1] not in ENV_KEYS:
                continue
            try:
                parts = shlex.split(match[2], comments=True)
            except ValueError as exc:
                raise ValueError(f".env 中 {match[1]} 的引号不完整") from exc
            values[match[1]] = " ".join(parts)
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    provider = values.get("RELEASE_AI_PROVIDER", "none").strip().lower()
    if provider not in {"none", "codex", "claude"}:
        raise ValueError("RELEASE_AI_PROVIDER 必须是 codex、claude 或 none")
    try:
        timeout = int(values.get("RELEASE_AI_TIMEOUT_SECONDS", "600"))
    except ValueError as exc:
        raise ValueError("RELEASE_AI_TIMEOUT_SECONDS 必须是正整数") from exc
    if timeout <= 0:
        raise ValueError("RELEASE_AI_TIMEOUT_SECONDS 必须是正整数")
    return AIConfig(
        provider, values.get("RELEASE_AI_MODEL", "").strip(), timeout,
    )


def git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Git {args[0]} 超时；未修改版本文件") from exc
    if result.returncode:
        # Do not dump subprocess stderr: local configuration may contain credentials.
        raise ValueError(f"Git {args[0]} 失败；请确认发布基准 tag 存在且属于当前 HEAD 历史")
    return result.stdout


def resolve_base_tag(previous: str, base_tag: str | None = None) -> str:
    base_tag = base_tag if base_tag is not None else f"v{previous}"
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-rc[1-9]\d*)?", base_tag):
        raise ValueError("--base-tag 必须是完整发布 tag，例如 v0.1.2-rc3")
    return base_tag


def validate_base_tag(root: Path, previous: str, base_tag: str | None = None) -> str:
    base_tag = resolve_base_tag(previous, base_tag)
    base = git(root, "rev-parse", "--verify", f"refs/tags/{base_tag}^{{commit}}").strip()
    git(root, "merge-base", "--is-ancestor", base, "HEAD")
    return base_tag


def make_prompt(version: str, previous: str, base_tag: str, target: str) -> str:
    return f"""在当前 ArchiveX 仓库中为 v{version} 编写中文发布说明，直接编辑 {target}。
先阅读 AGENTS.md 与 .agents 中的相关说明。自行查看 {base_tag} 到当前工作区的 Git
提交和差异（含待提交的新文件），按需阅读源码、测试、配置和历史发布说明。
在已有模板中填写主要变更，以及从 v{previous} 升级的兼容性、备份、停机、数据库
迁移和回滚要求；按实际代码判断，把用户可感知的变化组织成发布说明。
保持标题和模板的章节结构、镜像地址；保留顶部 RELEASE_TODO 人工审阅标记，
有待确认的要求也用 RELEASE_TODO 标记。验证情况依据实际证据填写。
本次任务的文件修改范围是 {target}，完成后简述你写入的内容。
"""


def validate_draft(content: str, version: str) -> None:
    if not content.startswith(f"# ArchiveX v{version} 发布说明\n"):
        raise ValueError("Agent 写入的发布说明标题与版本不一致")
    for heading in ("## 主要变更", "## 镜像", "## 升级", "## 发布验证"):
        section = re.search(rf"(?m)^{re.escape(heading)}[ \t]*\n([\s\S]*?)(?=^## |\Z)", content)
        if not section or not section[1].strip():
            raise ValueError(f"Agent 写入的发布说明缺少或未填写 {heading}")


def generate(
    root: Path, version: str, previous: str, config: AIConfig, *, template: str,
    base_tag: str | None = None,
) -> str:
    executable = shutil.which(config.provider)
    if not executable:
        raise ValueError(f"找不到 {config.provider} CLI；请安装登录或设置 RELEASE_AI_PROVIDER=none")
    base_tag = validate_base_tag(root, previous, base_tag)
    target = Path(".agents") / f"RELEASE_V{version.replace('-', '_').upper()}.md"
    path = root / target
    if path.exists() or path.is_symlink():
        raise ValueError(f"{target} 已存在；保留已有发布说明")
    prompt = make_prompt(version, previous, base_tag, target.as_posix())
    print(f"正在调用 {config.provider} 生成 {base_tag} → v{version} 的草稿（待人工审阅）...", flush=True)
    if config.provider == "codex":
        command = [
            executable, "-a", "never", "exec", "--sandbox", "workspace-write",
            "--cd", str(root), "--add-dir", str(path.parent), "--ephemeral", "--color", "never",
        ]
    else:
        command = [
            executable, "-p", "--output-format", "json", "--no-session-persistence",
            "--permission-mode", "acceptEdits", "--allowedTools",
            "Read", "Glob", "Grep", "Edit", "Write", "Bash(git:*)",
        ]
    if config.model:
        command.extend(["--model", config.model])
    if config.provider == "codex":
        command.append("-")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")
    try:
        try:
            result = subprocess.run(
                command, input=prompt, cwd=root, capture_output=True, text=True,
                encoding="utf-8", timeout=config.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"{config.provider} 生成超时（{config.timeout} 秒）；未修改版本文件") from exc
        if result.returncode:
            raise ValueError(f"{config.provider} 生成失败；请检查 CLI 登录/模型配置（未修改版本文件）")
        if config.provider == "claude":
            envelope = json.loads(result.stdout)
            if not isinstance(envelope, dict) or envelope.get("is_error"):
                raise ValueError("Claude 生成失败；未修改版本文件")
        if not path.is_file() or path.is_symlink():
            raise ValueError("Agent 未写入目标发布说明")
        content = path.read_text(encoding="utf-8")
        if content == template:
            raise ValueError("Agent 未填写发布说明模板")
        validate_draft(content, version)
        if "RELEASE_TODO" not in content:
            title, remainder = content.split("\n", 1)
            content = f"{title}\n\nRELEASE_TODO：请审阅 AI 生成的主要变更和升级/回滚要求。\n{remainder}"
            path.write_text(content, encoding="utf-8")
        return content
    except BaseException:
        # Only remove the new draft created by this invocation, never an existing note.
        path.unlink(missing_ok=True)
        raise
