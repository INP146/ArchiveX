# ArchiveX 数据库合并记录

- 日期：2026-08-09
- 目标：将归档业务表与任务生命周期表统一到 `ARCHIVE_DB_PATH`

## 当前布局

ArchiveX 自有数据只写入一个 SQLite 文件：

```text
archive.sqlite3
  accounts
  account_username_history
  posts
  media
  sync_runs
  queue_tasks
  queue_attempts
```

`twscrape/accounts.db` 继续独立存在。它保存 X 登录 cookie、代理、请求锁与统计，schema 和写入行为由 twscrape 管理，不属于 ArchiveX 业务数据库。

队列消息、延迟重试、去重锁和进程 heartbeat 仍存储在 Redis；Redis 是运行时队列基础设施，不替代 SQLite 中可查询的任务历史。

## 一次性数据处理

开发阶段直接完成数据和代码切换，不在运行时代码中保留迁移兼容层：

1. 停止旧 API、worker 和 scheduler，确认 Redis Stream 没有 pending 或 lag。
2. 使用 SQLite backup API 分别生成归档库和旧任务库的一致快照。
3. 将 `queue_tasks` 和 `queue_attempts` 一次性导入 `archive.sqlite3`。
4. 校验任务数量、`PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`。
5. 删除旧任务库及 WAL/SHM 伴随文件，只保留压缩归档。

迁移前快照位于：

```text
backups/pre-database-consolidation-20260809T112618Z.tar.gz
```

归档内含迁移时的 `archive.sqlite3` 和 `taskiq-dashboard.sqlite3`，两者均通过完整性检查。当前代码和活动数据不包含 `taskiq-dashboard` 路径、迁移标记表或自动导入逻辑。

## 后续任务关联

统一数据库为任务补充结构化目标字段和外键提供基础。后续可在 `queue_tasks` 增加 `account_x_user_id`、`media_id` 等可空字段，分别关联 `accounts` 和 `media`；不再需要解析 `args` 或做跨数据库查询来展示“同步谁”和“下载哪个媒体”。
