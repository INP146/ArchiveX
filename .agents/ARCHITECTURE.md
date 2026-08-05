# X 账号内容归档系统架构设计

## 1. 文档信息

| 项目     | 内容                                                       |
| -------- | ---------------------------------------------------------- |
| 文档状态 | 初版设计                                                   |
| 目标     | 自托管、定时归档指定 X 账号的公开内容和媒体                |
| 主要组件 | `twscrape`、`gallery-dl`、PostgreSQL、MinIO、Python Worker |
| 部署方式 | Docker Compose；后续可迁移到 Kubernetes                    |

参考项目：[`vladkens/twscrape`](https://github.com/vladkens/twscrape)、[`mikf/gallery-dl`](https://github.com/mikf/gallery-dl)。

## 2. 背景与目标

用户希望在关注的博主删除帖子、删除账号或停止运营前，保留一份可检索的个人归档，并定时更新增量。

### 2.1 目标

- 定时发现目标账号的新帖子、回复、转帖、引用帖及其媒体。
- 保存原始帖子 JSON、标准化字段、原始链接和抓取时间。
- 下载图片、视频等可访问媒体，并进行去重和完整性校验。
- 在内容后续不可访问时，标记删除/失效，但不自动删除已有归档。
- 支持断点续传、失败重试、任务可观测和手工重跑。
- 只允许私有访问，支持导出和异地备份。

## 3. 总体架构

```text
                         +------------------+
                         |  管理/检索 Web UI |
                         +---------+--------+
                                   |
                              +----v-----+
                              | FastAPI  |
                              +----+-----+
                                   |
              +--------------------+--------------------+
              |                                         |
       +------v-------+                          +------v------+
       | PostgreSQL   |                          |    MinIO    |
       | 元数据/状态   |                          | 图片/视频    |
       +--------------+                          +-------------+
              ^                                         ^
              |                                         |
       +------+--------+                         +-------+-------+
       | Scheduler     |                         | Media Worker  |
       | 定时/手工任务  +------------------------>| gallery-dl    |
       +------+--------+                         +---------------+
              |
       +------v--------+
       | Crawl Worker   |
       | twscrape       |
       +---------------+

    X 公共内容/媒体接口或页面
```

### 3.1 组件职责

| 组件         | 职责                                                                       |
| ------------ | -------------------------------------------------------------------------- |
| Scheduler    | 根据账号策略创建同步任务、删除复查任务和备份任务；保证同一账号不并发执行。 |
| Crawl Worker | 使用 `twscrape` 读取账号时间线和帖子详情，标准化数据并写入数据库。         |
| Media Worker | 根据数据库中的媒体任务调用 `gallery-dl` 下载文件，写入对象存储并校验哈希。 |
| PostgreSQL   | 保存账号、帖子、媒体元数据、游标、任务状态、错误和原始 JSON。              |
| MinIO/S3     | 持久化媒体文件和可选的原始响应文件。                                       |
| FastAPI      | 提供私有管理 API、搜索、浏览、任务重试和导出。                             |
| Web UI       | 查看账号同步状态、按时间/关键词/媒体筛选和查看归档内容。                   |
| Caddy/Nginx  | HTTPS、反向代理和基础访问控制。                                            |

## 4. 抓取与归档流程

### 4.1 首次全量导入

1. 管理员添加目标账号的用户名或稳定账号 ID。
2. Worker 解析账号信息并保存账号 ID，避免用户名改名导致重复账号。
3. 按分页拉取历史帖子；每页成功后立即提交事务并记录游标。
4. 对每条帖子执行幂等写入：以 `tweet_id` 作为唯一键。
5. 将媒体 URL 写入 `media_assets`，再由 Media Worker 异步下载。
6. 达到配置的历史时间范围或平台返回的最早边界后结束全量任务。

全量任务应具备暂停和恢复能力，不能在内存中积累全部结果。

### 4.2 增量同步

1. Scheduler 按账号的 `sync_interval` 创建任务。
2. Worker 从上次成功游标继续拉取，默认只查询新增内容。
3. 新帖子先保存原始 JSON 和标准字段，再创建媒体下载任务。
4. 成功完成当前批次后更新游标；失败时保留旧游标，下一轮重试。
5. 对近期内容执行详情复查，补齐编辑后的文本、互动数快照和媒体。

建议默认频率：普通账号每 6 小时一次，活跃账号每 1-2 小时一次。实际频率应受平台限制和个人使用场景约束。

### 4.3 删除和失效检测

抓取不到某条帖子不能立即判定为已删除，原因可能是临时错误、分页不完整、接口限流或媒体链接过期。

- 只复查过去 30-90 天内的归档帖子。
- 连续 2-3 次详情查询确认不可访问后，设置 `availability = deleted_or_unavailable`。
- 保存 `last_checked_at`、失败原因和检查次数。
- 不删除数据库记录或媒体文件。
- 账号整体不可访问时，标记账号状态，避免把一次服务故障误判为账号销号。

## 5. `twscrape` 与 `gallery-dl` 的边界

`twscrape` 是发现和读取帖子数据的适配器，负责账号时间线、帖子详情、分页和响应解析。它不负责归档生命周期，也不应直接决定本地文件是否删除。

`gallery-dl` 是媒体下载适配器，负责从帖子 URL 或媒体 URL 下载附件、重试和文件命名。下载结果必须回写数据库，由系统统一管理状态、哈希、路径和错误。

建议将两者包在内部接口后面，以便未来替换为官方 API 或其他抓取器：

```python
class PostSource(Protocol):
    def fetch_account(self, account_id: str, cursor: str | None) -> FetchPage: ...
    def fetch_post(self, post_id: str) -> PostSnapshot: ...

class MediaDownloader(Protocol):
    def download(self, post_url: str, target_dir: str) -> DownloadResult: ...
```

凭证、cookie 或 session 文件只在 Worker 中使用，不通过 Web API 返回，也不写入日志。

## 6. 数据模型

### 6.1 `accounts`

```text
id                  UUID / bigint       内部主键
x_user_id           text UNIQUE         X 稳定用户 ID
username            text                最近一次用户名
display_name        text
status              active/paused/error/unavailable
sync_interval_sec   integer
cursor              text                增量分页游标
oldest_captured_at  timestamptz
last_success_at     timestamptz
created_at           timestamptz
updated_at           timestamptz
```

### 6.2 `posts`

```text
id                  bigint PRIMARY KEY   X 帖子 ID
account_id          FK accounts.id
conversation_id     bigint NULL
post_type           original/reply/repost/quote
text                text
posted_at           timestamptz
permalink           text
raw_payload         jsonb                 原始响应，便于重解析
availability        available/deleted_or_unavailable/unknown
first_seen_at       timestamptz
last_seen_at        timestamptz
last_checked_at     timestamptz
```

### 6.3 `media_assets`

```text
id                  UUID
post_id             FK posts.id
source_url          text
media_type          image/video/gif/other
sha256              text NULL
object_key          text NULL
size_bytes          bigint NULL
download_status     pending/downloading/completed/failed/skipped
attempt_count       integer
last_error          text NULL
created_at          timestamptz
updated_at          timestamptz
```

### 6.4 `jobs` 与 `sync_runs`

任务表保存任务类型、目标账号/帖子、状态、重试次数、锁定时间和错误信息。同步运行表保存每次运行的开始/结束时间、抓取数量、下载数量、跳过数量和错误摘要，用于审计和监控。

建议索引：

- `posts(account_id, posted_at DESC)`
- `posts(to_tsvector('simple', text))` 或独立搜索引擎索引
- `media_assets(download_status, updated_at)`
- `sync_runs(account_id, started_at DESC)`

## 7. 幂等性、去重与一致性

- 帖子以 X `tweet_id` 唯一约束幂等写入；重复抓取只更新快照字段。
- 媒体以 `post_id + source_url` 去重，下载完成后以 SHA-256 做内容级去重。
- 数据库事务只覆盖元数据和任务状态，不把大文件写入事务。
- 媒体采用临时文件下载，校验成功后原子移动到最终对象键，避免产生半文件。
- Worker 使用租约或数据库行锁，防止同一任务被多个进程重复执行。
- 任务至少一次执行；所有副作用操作必须可重入。

## 8. 失败处理与恢复

### 8.1 重试策略

- 网络错误、5xx、临时限流：指数退避，最多 5 次。
- 明确的 401/403 或凭证失效：暂停该账号任务并报警，不盲目重试。
- 媒体 404：记录为 `unavailable`，保留帖子元数据。
- 解析错误：保存原始响应，进入死信任务，等待代码升级后重放。

### 8.2 游标安全

只有当前页和该页产生的帖子都完成持久化后，才能提交下一页游标。进程崩溃后从旧游标重放不会造成重复数据。

### 8.3 备份与恢复

- PostgreSQL 每日逻辑备份，至少保留 30 天。
- MinIO 使用对象版本或 `restic` 做增量、加密、异地备份。
- 每月抽样恢复数据库和媒体，验证备份不是“只写不读”。
- 备份密钥与 X session 凭证分开管理。

## 9. API 与 Web 功能

首版只需提供私有 API：

```text
GET    /api/accounts
POST   /api/accounts
PATCH  /api/accounts/{id}
POST   /api/accounts/{id}/sync
GET    /api/posts?account_id=&q=&from=&to=&has_media=
GET    /api/posts/{tweet_id}
POST   /api/posts/{tweet_id}/recheck
GET    /api/jobs
POST   /api/jobs/{id}/retry
GET    /api/exports/{account_id}
```

Web 页面应至少展示：账号状态、最近成功同步时间、待下载数量、失败任务、帖子时间线、原帖链接、媒体预览和删除/失效标记。

## 10. 部署方案

### 10.1 Docker Compose 服务

```text
postgres       PostgreSQL 16
minio          对象存储
api            FastAPI + Uvicorn
worker-crawl   twscrape 抓取进程
worker-media   gallery-dl 媒体下载进程
scheduler      定时任务进程
caddy          HTTPS 和访问控制
```

所有服务使用非 root 用户运行；数据库、对象存储和 session 文件不直接暴露公网。媒体目录应挂载到独立磁盘，并设置容量告警。

### 10.2 配置与密钥

通过 `.env` 或 Docker secrets 注入：

```text
DATABASE_URL
S3_ENDPOINT / S3_ACCESS_KEY / S3_SECRET_KEY / S3_BUCKET
ARCHIVE_ADMIN_USERNAME / ARCHIVE_ADMIN_PASSWORD_HASH
X_SESSION_PATH
```

`.env`、cookie、session 文件、备份密钥不得提交到 Git。生产环境应定期轮换管理员凭证和存储密钥。

## 11. 安全、隐私与合规

- 仅归档公开内容，并将系统限制为个人私有用途。
- 通过 HTTPS、强密码和可选的 TOTP 保护管理界面。
- 日志脱敏，不记录 cookie、Authorization header、完整媒体签名 URL。
- 对媒体和数据库启用磁盘/对象存储加密；备份使用独立密钥。
- 设置保留策略、导出和彻底删除功能，便于处理不再需要的内容。
- 遵守 X 的服务条款、robots/访问限制和适用的版权、隐私法律；对外分享前应取得权利人许可。

## 12. 可观测性

记录结构化日志和指标：

- `sync_success_total`、`sync_failure_total`
- `posts_discovered_total`、`posts_new_total`
- `media_completed_total`、`media_failed_total`
- `sync_lag_seconds`
- `job_queue_depth`
- `storage_bytes_used`

当某账号连续失败、凭证失效、队列积压、磁盘超过 80% 或备份失败时发送通知。首版可使用 Prometheus + Grafana，也可以先使用结构化日志和 Uptime Kuma。

## 13. 分阶段实施

### Phase 1：最小可用归档

- Docker Compose、PostgreSQL、MinIO。
- `twscrape` 增量抓取原始帖子 JSON。
- `gallery-dl` 下载图片/视频。
- 简单 CLI：添加账号、手工同步、查看失败任务。

### Phase 2：可靠性与检索

- 首次全量导入、游标恢复、指数退避和死信任务。
- 删除/失效复查。
- FastAPI + Web UI、全文搜索、媒体预览。
- 数据库和对象存储备份。

### Phase 3：长期运维

- 指标、告警、备份恢复演练。
- 可替换的 `PostSource` 适配器，支持官方 API 或其他实现。
- 导出为 JSONL/HTML/静态站点。
- 多设备访问控制和更细粒度的保留策略。

## 14. 主要风险与应对

| 风险                | 影响         | 应对                                           |
| ------------------- | ------------ | ---------------------------------------------- |
| X 接口或页面变化    | 抓取中断     | 封装适配器；保存原始响应；保守升级依赖。       |
| 账号被限流/要求验证 | 同步延迟     | 降低频率；指数退避；凭证失效时人工处理。       |
| 媒体 URL 过期       | 媒体无法补抓 | 发现帖子后尽快下载；失败时保留帖子 JSON。      |
| 误判删除            | 状态错误     | 多次复查和区分服务故障/权限错误。              |
| 磁盘耗尽            | 服务停止     | 容量告警、媒体配额、备份和归档策略。           |
| 凭证泄露            | 账号安全事故 | secrets 管理、日志脱敏、最小权限、定期轮换。   |
| 版权或隐私争议      | 法律风险     | 限制个人私用，公开内容范围最小化，不对外传播。 |

## 15. 验收标准

- 重启任一 Worker 后，未完成任务可以自动恢复，不产生重复帖子或半文件。
- 同一帖子重复抓取不会新增数据库记录，媒体哈希可识别重复文件。
- 网络错误和临时限流会退避重试，明确凭证错误会暂停并报警。
- 帖子不可访问时，系统能在多次复查后标记状态，且本地归档不被删除。
- 可从备份恢复数据库和至少一条完整帖子的媒体。
- Web UI 不暴露 session、cookie 或存储密钥，未经认证不能访问。
