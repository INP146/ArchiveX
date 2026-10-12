# ArchiveX 自动发布流程

## 日常发版

需要决定版本号并确认本版本的升级要求。脚本兼容 Python 3.9+，无需启动服务。
下面用 `0.1.2-rc4` 举例；正式版同样使用此流程，只需去掉 `-rc4`。

在仓库根目录 `.env` 配置一次，之后不需要在命令里重复输入 AI 参数：

```dotenv
RELEASE_AI_PROVIDER=codex
RELEASE_AI_MODEL=
RELEASE_AI_TIMEOUT_SECONDS=600
```

`RELEASE_AI_PROVIDER` 支持 `codex`、`claude`、`none`；`none` 只生成固定模板。
模型留空时沿用本机 CLI 配置，不另设 API key。超时单位为秒。
Shell 已设置的同名变量优先于 `.env`，不做 `$VAR` 或命令插值。
没有 `.env` 和对应环境变量时默认模板模式；CI 的 `check` 不读取或调用 AI。

```sh
python3 scripts/release.py prepare 0.1.2-rc4 --dry-run
python3 scripts/release.py prepare 0.1.2-rc4
```

比较基准默认使用准备前当前版本对应的 tag，例如 `v0.1.2-rc3`。这是本次发布的
参数，不保存在 `.env`；需要调整时使用可选的 `--base-tag`：

```sh
python3 scripts/release.py prepare 0.1.2-rc4 --base-tag v0.1.2-rc2
```

参数必须是完整已存在且属于当前 HEAD 历史的发布 tag。日常发版省略即可。

准备脚本同步以下内容：

- `pyproject.toml`：Python 的 `0.1.2rc4` 格式。
- `frontend/package.json` 和 `frontend/package-lock.json`：`0.1.2-rc4`。
- `docker-compose.ghcr.yml`：全部后端和 Web 镜像标签。
- README 的 Published container images 部分；历史迁移说明保持原有版本。
- 新建 `.agents/RELEASE_V0.1.2_RC4.md` 发布说明草稿。

AI CLI 的工作目录就是仓库根目录。Agent 先阅读 `AGENTS.md` 和 `.agents` 相关文档，
自行查看基准 tag 到当前工作区的提交与差异，按需阅读源码、测试、配置和历史发布
说明，并直接填写目标 `.agents/RELEASE_*.md`。包含待提交改动，发版前需要将其提交。
脚本只验证基准 tag、准备模板和调用 agent，不预先打包差异，不设材料长度上限。
基准 tag 缺失或不在当前 HEAD 历史中时会拒绝生成。

Codex 使用 workspace-write，Claude 允许读取、查找、编辑和 Git 工具；两者在项目
目录中非交互运行，遵守仓库的进程管理等约定。本次 agent 的修改任务是目标发布
说明，版本文件由脚本同步。脚本读取实际写入的文档，检查标题与必需章节并保留
人工审阅标记；stdout 只用于 CLI 状态，不作为发布说明内容。
生成失败或超时时移除本次新建的草稿，版本文件尚未更新；已有发布说明保留。
`--dry-run` 仅验证配置和基准 tag，不调用模型、不写文件。
实际模型调用按各自 CLI 的登录、服务和用量配置运行。

脚本不修改依赖版本、本地 `.env` 或本地 `docker-compose.yml`，不启动前后端服务，也不
提交、打 tag 或推送。新版本必须高于当前版本；已有发布说明不会被覆盖。
再次检查使用 `check`，不要对相同版本重复运行 `prepare`。

审阅新发布说明：核对主要变更，以及升级是否涉及数据库迁移、备份、停机和回滚
限制。无数据库迁移也需要明确说明。AI 草稿始终带人工审阅 `RELEASE_TODO` 标记，
不确定的要求也保留此标记；完成审阅并替换全部标记后，才能通过发布检查：

```sh
python3 scripts/release.py check --require-notes
git diff
git status --short
```

审查并提交本次代码与版本变更，确保用于发布的工作区修改都已提交。之后执行：

```sh
git push origin main
git tag -a v0.1.2-rc4 -m "ArchiveX v0.1.2-rc4"
git push origin v0.1.2-rc4
```

第一次使用时，要把本次新增的脚本、测试和 workflows 一起提交。现有版本仍为
rc3，此次引入自动化本身不代表发布 rc4。

## Actions 执行顺序

`validate.yml` 在 PR、main 推送时运行，也由 tag 发布流程调用。校验包括版本一致性、
锁定的 Python 运行时依赖安装、依赖冲突检查、完整后端测试、前端 TypeScript 和生产
构建，以及构建版/GHCR 版 Compose（含 tools profile）的配置校验。
tag 发布额外要求 tag 对应源码版本、发布说明存在且不再包含草稿占位符。

`publish-container-images.yml` 的 Publish release 流程：

1. 调用上述校验；失败时不发布任何镜像。
2. 并行构建后端和 Web 镜像，发布到 GHCR，包含 linux/amd64、linux/arm64。
3. 从 GHCR 读取两个已发布版本的 manifest，确认两种架构齐全。
4. 使用对应 `.agents/RELEASE_*.md` 创建 GitHub Release，附上
   `docker-compose.ghcr.yml` 和 `SHA256SUMS`。

RC Release 自动标记 prerelease，不修改稳定版 `latest` 镜像标签，也不成为 GitHub
Latest Release。正式版更新 `latest`。仓库名按现有流程转为小写，不额外需要 PAT；
使用 GitHub 自带的 `GITHUB_TOKEN`，按 job 授予 contents/packages 所需权限。
仓库或组织策略需要允许 Actions 发布 GHCR 镜像、创建 Release。

## 失败与重跑

进入 GitHub Actions 的 Publish release 查看失败步骤并使用 Re-run failed jobs。
两个镜像构建是独立任务：如果其中一个失败，另一个镜像可能已发布，但不会创建
GitHub Release。重跑失败任务完成后，再运行后续 manifest 校验和 Release 创建。
已存在的 GitHub Release 会保留说明与资产，不会重复创建或覆盖人工修改。

如果版本或发布说明不正确，修正后使用下一个 RC 版本；不要移动已发布的 tag。
已发布 tag 对应的源码不会随 main 上的修复变化。

## 部署

发布完成后，人工决定何时升级。从该版本 GitHub Release 下载 Compose 资产，
在同一目录可用 `sha256sum -c SHA256SUMS` 校验（macOS 使用 `shasum -a 256 -c SHA256SUMS`）。
把新版镜像标签合并到本地部署配置，保留原有令牌、端口、挂载和自定义设置；不要
直接用发布模板覆盖已有配置。按该版本的升级说明完成备份、必要迁移和服务启停。

流水线不会连接生产机器、执行迁移或启动/停止生产服务。旧版升级要求继续参考
对应历史发布说明，例如 rc2 → rc3 必须进行 schema v2 → v3 离线迁移。
