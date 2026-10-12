#!/usr/bin/env python3
"""Prepare and validate releases without committing, tagging, or starting services.

Only the standard library is required; this helper also runs on macOS Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Keep imports usable both as a CLI and via the existing importlib-based tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_ai


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc([1-9]\d*))?")
PROJECT_VERSION_RE = re.compile(
    r'(?ms)(^\[project\]\s*\n(?:(?!^\[).)*?^version\s*=\s*")([^"\n]+)(")'
)
IMAGE_RE = re.compile(r"(?m)^(\s*image:\s*)(ghcr\.io/[\w.-]+/archivex(?:-web)?):([^\s#]+)")
README_HEADING = "## Published container images\n"
TODO = "RELEASE_TODO"


def normalize_version(value: str) -> str:
    version = value.removeprefix("v")
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError("版本格式必须是 0.1.2-rc4 或 0.1.2（可带 v 前缀）")
    return version


def version_key(version: str) -> tuple[int, int, int, float]:
    match = VERSION_RE.fullmatch(normalize_version(version))
    assert match is not None
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), int(rc) if rc else float("inf")


def python_version(version: str) -> str:
    return version.replace("-rc", "rc")


def notes_path(root: Path, version: str) -> Path:
    return root / ".agents" / f"RELEASE_V{version.replace('-', '_').upper()}.md"


def readme_section(text: str) -> tuple[str, str, str]:
    before, heading, rest = text.partition(README_HEADING)
    if not heading:
        raise ValueError("README 缺少 Published container images 部分")
    section, next_heading, after = rest.partition("\n## ")
    return before + heading, section, next_heading + after


def current_version(root: Path) -> str:
    return normalize_version(json.loads((root / "frontend/package.json").read_text())["version"])


def check(root: Path, version: str, *, require_notes: bool = False) -> None:
    """Reject inconsistent metadata before any image can be published."""
    version = normalize_version(version)
    errors = []
    project = PROJECT_VERSION_RE.search((root / "pyproject.toml").read_text())
    if not project or project[2] != python_version(version):
        errors.append(f"pyproject.toml 应为 {python_version(version)}")
    for filename in ("frontend/package.json", "frontend/package-lock.json"):
        data = json.loads((root / filename).read_text())
        if data.get("version") != version:
            errors.append(f"{filename} 应为 {version}")
        if filename.endswith("package-lock.json"):
            if data.get("packages", {}).get("", {}).get("version") != version:
                errors.append(f"{filename} 的根包版本应为 {version}")
    images = IMAGE_RE.findall((root / "docker-compose.ghcr.yml").read_text())
    if not images or not any(image.endswith("/archivex-web") for _, image, _ in images):
        errors.append("GHCR Compose 缺少 ArchiveX 镜像")
    if not any(image.endswith("/archivex") for _, image, _ in images):
        errors.append("GHCR Compose 缺少后端镜像")
    for _, image, actual in images:
        if actual != version:
            errors.append(f"Compose 中 {image}:{actual} 应为 :{version}")
    _, section, _ = readme_section((root / "README.md").read_text())
    documented_versions = re.findall(r"\b(?:v)?(\d+\.\d+\.\d+(?:-rc\d+)?)\b", section)
    if not documented_versions or set(documented_versions) != {version}:
        errors.append(f"README 镜像发布部分应统一使用 {version}")
    if require_notes:
        path = notes_path(root, version)
        if not path.is_file():
            errors.append(f"缺少发布说明 {path.relative_to(root)}")
        else:
            notes = path.read_text()
            if not notes.startswith(f"# ArchiveX v{version} 发布说明\n"):
                errors.append("发布说明标题与版本号不一致")
            if TODO in notes or not notes.strip():
                errors.append(f"请完成 {path.relative_to(root)} 中的 {TODO} 草稿内容")
            for heading in ("## 主要变更", "## 升级"):
                section = re.search(
                    rf"(?m)^{re.escape(heading)}[ \t]*\n([\s\S]*?)(?=^## |\Z)", notes
                )
                if not section or not section[1].strip():
                    errors.append(f"发布说明缺少或未填写 {heading}")
    if errors:
        raise ValueError("\n".join(errors))


def draft_notes(
    version: str, previous: str, images: list[str], *, base_tag: str | None = None,
    ai_review: bool = False,
) -> str:
    image_lines = "\n".join(f"{image}:{version}" for image in images)
    changes = f"{TODO}：填写相对 {base_tag or f'v{previous}'} 的主要变更，删除此占位行。"
    upgrade = (
        f"{TODO}：说明从 v{previous} 升级是否需要数据库迁移、备份与停机步骤，以及\n"
        "回滚限制；如果无需迁移，也请明确说明。删除此占位段落后才能发布。"
    )
    review = ""
    if ai_review:
        review = (
            f"\n{TODO}：以下草稿由本地 AI CLI 基于 {base_tag or f'v{previous}'} 到当前工作区的变更生成。\n"
            "请核对主要变更和升级/回滚要求，完成审阅后删除此段及其他待确认标记。\n"
        )
    return f"""# ArchiveX v{version} 发布说明
{review}

## 主要变更

{changes}

## 镜像

```text
{image_lines}
```

CI 校验通过后自动构建并发布 linux/amd64、linux/arm64 镜像。

## 升级

{upgrade}

从 GitHub Release 下载本版本 docker-compose.ghcr.yml。将镜像版本合并到本地
docker-compose.yml，保留认证令牌、端口和存储挂载等本地配置，再按上述升级步骤操作。

## 发布验证

推送版本 tag 后，Actions 依次执行版本校验、后端测试、前端生产构建和 Compose
配置校验。两个镜像发布成功且均包含 amd64/arm64 manifest 后，创建 GitHub Release
并附上 Compose 文件及 SHA256SUMS。RC 标记为 prerelease，不更新 latest。
"""


def prepare(
    root: Path, version: str, *, dry_run: bool = False, base_tag: str | None = None
) -> list[Path]:
    version = normalize_version(version)
    previous = current_version(root)
    release_ai.resolve_base_tag(previous, base_tag)
    check(root, previous)
    if version_key(version) <= version_key(previous):
        raise ValueError(f"新版本必须高于当前版本 {previous}")
    path = notes_path(root, version)
    if path.exists():
        raise ValueError(f"{path.relative_to(root)} 已存在；为保留已有说明，未修改任何文件")

    changes = {}
    project_path = root / "pyproject.toml"
    changes[project_path] = PROJECT_VERSION_RE.sub(
        lambda match: match[1] + python_version(version) + match[3], project_path.read_text(), count=1
    )
    for filename in ("frontend/package.json", "frontend/package-lock.json"):
        file_path = root / filename
        # Change only root version fields, preserving dependency versions and formatting.
        text = file_path.read_text()
        data = json.loads(text)
        data["version"] = version
        if filename.endswith("package-lock.json"):
            data["packages"][""]["version"] = version
        updated = re.sub(
            r'(?m)^(  "version": ")[^"]+("[,]?)$',
            lambda match: match[1] + version + match[2], text, count=1
        )
        if filename.endswith("package-lock.json"):
            updated = re.sub(
                r'(?m)^(      "version": ")[^"]+("[,]?)$',
                lambda match: match[1] + version + match[2], updated, count=1
            )
        if json.loads(updated) != data:
            raise ValueError(f"{filename} 格式已变化，无法安全更新；未修改任何文件")
        changes[file_path] = updated
    compose_path = root / "docker-compose.ghcr.yml"
    compose = compose_path.read_text()
    images = sorted({image for _, image, _ in IMAGE_RE.findall(compose)})
    changes[compose_path] = IMAGE_RE.sub(lambda match: f"{match[1]}{match[2]}:{version}", compose)
    readme_path = root / "README.md"
    before, section, after = readme_section(readme_path.read_text())
    changes[readme_path] = before + section.replace(previous, version) + after
    config = release_ai.load_config(root)
    notes = draft_notes(version, previous, images, base_tag=base_tag, ai_review=config.provider != "none")
    if config.provider != "none":
        if dry_run:
            release_ai.validate_base_tag(root, previous, base_tag)
            print(f"将调用 {config.provider} 生成草稿（dry-run 不调用模型）")
        else:
            notes = release_ai.generate(root, version, previous, config, template=notes, base_tag=base_tag)
    changes[path] = notes
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        for file_path, content in changes.items():
            file_path.write_text(content)
        check(root, version)
    return list(changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="同步版本并生成发布说明草稿")
    prepare_parser.add_argument("version")
    prepare_parser.add_argument("--dry-run", action="store_true", help="只显示计划修改的文件")
    prepare_parser.add_argument("--base-tag", help="本次生成的比较基准；默认使用准备前当前版本的 tag")
    check_parser = commands.add_parser("check", help="校验版本，可传入待发布 tag")
    check_parser.add_argument("version", nargs="?")
    check_parser.add_argument("--require-notes", action="store_true", help="要求发布说明已完成")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            paths = prepare(ROOT, args.version, dry_run=args.dry_run, base_tag=args.base_tag)
            for path in paths:
                print(f"{'将修改' if args.dry_run else '已准备'} {path.relative_to(ROOT)}")
            print("请填写发布说明，然后运行 python3 scripts/release.py check --require-notes")
        else:
            version = normalize_version(args.version) if args.version else current_version(ROOT)
            check(ROOT, version, require_notes=args.require_notes)
            print(f"版本校验通过：v{version}")
    except (ValueError, OSError, KeyError) as exc:
        print(f"发布准备/校验失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
