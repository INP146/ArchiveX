# ArchiveX v0.1.2-rc3 发布说明

## 发布目的

`v0.1.2-rc3` 将 ArchiveX 的帖子归档模型升级到 schema v3，修复 repost 为每个
事件重复保存和下载原帖媒体导致归档盘快速耗尽的问题。repost 现在保存独立事件，
多个 repost 共享同一条 origin 内容和媒体记录；quote、reply、外部作者和归档账号
观察关系也分别建模。

## 主要变更

- 使用 `x_users`、`observed_accounts`、`posts`、`reposts` 和 observation 关系表达
  内容作者、独立转发事件以及哪个归档账号发现了内容。
- repost 不再复制正文或媒体；同一 origin 的媒体只保留一个 canonical 记录和下载任务，
  自转仍保留独立 repost 事件。
- quote 的外层正文、引用内容和媒体分开保存；reply 目标可以是外部作者，只有 ID 时
  使用 reference-only 占位，不伪造作者身份。
- API 和 Web 时间线返回结构化 `author`、`reference`、`origin`、`reposter`，混合分页
  按事件时间排序；媒体筛选不会把 reply 目标的媒体算到回复上。
- 新增离线 v2 → v3 迁移工具，保留原始 JSON、同步历史、任务和 attempts，并在候选库
  未完成、队列仍有活动任务或源数据发生变化时拒绝安装。
- 新增低空间迁移模式：逐文件验证媒体 SHA-256，在同一文件系统内原地移动 canonical
  文件，只删除已确认的重复文件，避免为迁移再复制一份完整媒体归档。
- 修复迁移中断恢复、文件原子安装、回复用户名回退和未知作者身份污染问题。

## 镜像

```text
ghcr.io/inp146/archivex:0.1.2-rc3
ghcr.io/inp146/archivex-web:0.1.2-rc3
```

两个镜像均由 `v0.1.2-rc3` tag 触发 GitHub Actions 构建，并发布
`linux/amd64`、`linux/arm64` manifest。

## 升级

这是 SQLite schema v2 → v3 的离线迁移。先停止 scheduler、crawl worker、media worker
和 API，确认数据库没有活动任务并排空 Redis 投递。不要使用 `docker compose down -v`，
也不要在迁移期间启动新 worker。

磁盘空间充足时，先按完整备份流程演练迁移，再检查 `report.json`：

```sh
python -m archivex.migrate \
  --database /data/archive.sqlite3 \
  --archive-dir /data/archive \
  --output-dir /maintenance/post-model-v3
python -m archivex.migrate --resume-from /maintenance/post-model-v3
```

归档盘无法容纳第二份媒体时，使用低空间迁移工具。准备阶段只备份数据库和原始
JSON，并逐文件核验媒体，不复制整份媒体；应用阶段在同一归档文件系统内移动保留
文件，再删除哈希已确认的重复文件：

```sh
python -m archivex.migrate_compact \
  --database /data/archive.sqlite3 \
  --archive-dir /data/archive \
  --session-database /data/twscrape/accounts.db \
  --output-dir /maintenance/post-model-v3
python -m archivex.migrate_compact --resume-from /maintenance/post-model-v3
```

确认迁移验证通过后，下载与 tag 匹配的 Compose 文件并启动：

```sh
curl -fsSL https://raw.githubusercontent.com/INP146/ArchiveX/v0.1.2-rc3/docker-compose.ghcr.yml \
  -o ./docker-compose.yml
docker compose pull
docker compose up -d --remove-orphans
docker compose ps
```

部署后先检查 `/ready`、账号数量、任务历史、repost origin 和媒体读取。rc3 应用不会
接受仍为 v2 的业务数据库；回滚前必须停止所有写入，并确认迁移后没有新的数据库写入。

## 发布验证

- [x] 后端完整测试通过（174 项）。
- [x] 前端 TypeScript 检查和生产构建通过。
- [x] Python 依赖检查通过。
- [x] 构建版和 GHCR 版 Compose 配置校验通过。
- [x] 后端与 Web Docker 镜像构建并发布成功。
- [x] GHCR 中两个镜像的 `amd64/arm64` manifest 可用。
- [x] 推送 `main` 和带注释的 `v0.1.2-rc3` tag。
- [x] 正式 Docker 部署完成 schema v2 → v3 迁移并通过 SQLite、外键和媒体哈希校验。
- [x] 正式服务 `/ready` 返回健康，Web、repost/origin、quote/reference、任务历史和
  媒体读取验证通过。
