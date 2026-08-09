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

## 任务业务关联

`queue_tasks` 已使用统一数据库建立结构化任务对象：

- `account_x_user_id` 外键关联 `accounts`，表示账号同步目标，也保存媒体所属账号。
- `media_id` 外键关联 `media`，表示媒体下载目标；帖子和账号通过 `media -> posts -> accounts` 获取。
- `parent_task_id` 自关联产生媒体任务的同步任务。
- `retry_of` 自关联被手动重试的旧逻辑任务，与父任务语义分离。
- `trigger` 保存手动、定时、新增账号、重新执行等触发来源。
- `context` 保存入队时的账号、帖子和媒体展示快照。

外键用于当前业务对象的完整性和跳转，`context` 用于保留历史语义。账号改名、媒体状态变化或业务记录删除后，任务历史仍可说明入队时处理的对象。原始 `args`、`kwargs` 和 `labels` 继续用于执行排障，但不再参与任务对象识别、重跑、失败重试或任务搜索。

媒体任务首次由账号同步任务派生；自动重试保持同一逻辑任务 ID。任务中心手动重跑会创建新的媒体逻辑任务，同时复制原 `parent_task_id` 并通过 `retry_of` 指向旧媒体任务。同步任务详情按 `parent_task_id` 汇总派生媒体任务状态。

实施时 `queue_tasks` 和 `queue_attempts` 均为空，因此直接删除并按最终 schema 重建这两张表，没有生成迁移标记、兼容分支或临时数据，也没有改动账号、帖子、媒体和同步运行数据。

## 任务历史清理

任务中心通过 `DELETE /api/task-center/tasks/history` 清理所有终态历史，也可以用 `status=completed|failure|abandoned` 只清一种状态。删除 `queue_tasks` 时由外键级联删除对应的 `queue_attempts`。

`queued`、`in_progress` 和 `retry_scheduled` 不属于可清理状态，API 参数校验和仓储层都会拒绝删除。该入口不清理 Redis Stream、pending 消息、延迟重试或去重锁。
