# X 账号归档系统首版计划

## 1. 首版目标

首版只解决一件事：定时抓取环境变量中配置的 X 账号，将帖子元数据和媒体保存到本地，并通过一个只读 Web 面板查看归档。

首版不做后台配置页面、不做复杂任务队列、不引入 PostgreSQL/MinIO/Kubernetes，也不把运维能力塞进 Web 面板。修改账号、同步频率和存储位置时，修改环境变量并重启服务即可。

## 2. 首版范围

### 2.1 必须支持

- 从环境变量读取多个目标账号。
- 首次启动时抓取账号的近期历史内容。
- 按固定间隔增量抓取新帖子。
- 保存原创帖、回复、转帖和引用帖的基本信息。
- 使用 `gallery-dl` 下载图片、视频等可访问媒体。
- 以 X 帖子 ID 去重，服务重启后可以继续运行。
- 在 Web 面板中按账号和时间查看帖子，打开媒体和原帖链接。
- 记录最近一次同步时间、抓取数量和错误信息。
- 目标账号、抓取频率、历史范围和目录均由环境变量控制。

### 2.2 首版明确不做

- Web 面板中添加/删除账号或修改配置。
- 用户登录体系、多人权限和公开分享。
- 全文搜索、复杂筛选、推荐算法和统计图表。
- 删除检测、账号销号判定和内容版本对比。
- PostgreSQL、MinIO、Redis、Celery、Kubernetes 等外部基础设施。
- 自动代理、账号池、验证码绕过和高并发抓取。
- 自动异地备份；首版只提供可复制的本地数据目录。

删除检测和对象存储仍保留在长期架构文档中，待首版稳定后再做。

## 3. 首版架构

```text
                 +----------------------+
                 |  浏览器               |
                 +----------+-----------+
                            |
                       HTTP | 只读页面
                            v
                 +----------+-----------+
                 | Python App            |
                 | FastAPI + Jinja2      |
                 | 后台同步循环           |
                 +-----+------------+-----+
                       |            |
                 SQLite DB      本地文件
                 帖子/状态       媒体/原始 JSON
                       |
                +------+------+
                | twscrape   | 读取 X 帖子
                | gallery-dl | 下载媒体
                +-------------+
```

### 3.1 运行模型

首版只运行一个应用容器和一个进程：

- FastAPI 提供只读 Web 页面。
- 应用启动后台同步循环，按账号顺序执行抓取。
- 同步循环使用单进程锁，避免同一账号并发抓取。
- SQLite 保存索引和任务状态；大文件直接写入本地归档目录。
- 进程重启后根据数据库中的帖子 ID 和同步状态继续工作。

如果后续下载媒体明显拖慢页面响应，再拆分 `crawl-worker` 和 `media-worker`，首版不提前引入队列系统。

## 4. 环境变量

建议提供 `.env.example`，但不把真实凭证提交到仓库。

```dotenv
# 目标账号，逗号分隔；建议填写稳定用户名，程序启动时解析为 X 用户 ID
ARCHIVE_ACCOUNTS=account_a,account_b

# twscrape 使用的登录状态/账号数据库路径
TWSCRAPE_SESSION_PATH=/data/twscrape

# 数据库和文件目录
ARCHIVE_DB_PATH=/data/archive.sqlite3
ARCHIVE_DATA_DIR=/data/archive

# 首次导入和定时同步
ARCHIVE_INITIAL_LOOKBACK_DAYS=30
ARCHIVE_SYNC_INTERVAL_SECONDS=21600
ARCHIVE_TIMEZONE=Asia/Shanghai

# 是否下载媒体，以及单文件大小限制（0 表示不限制）
ARCHIVE_MEDIA_ENABLED=true
ARCHIVE_MEDIA_MAX_BYTES=0

# Web 服务
WEB_HOST=0.0.0.0
WEB_PORT=8000
WEB_AUTH_TOKEN=change-me
LOG_LEVEL=INFO
```

### 4.1 配置规则

- 必填项缺失时服务启动失败，并给出明确错误。
- `ARCHIVE_ACCOUNTS` 为空时不启动抓取，但 Web 面板仍可打开查看已有数据。
- 账号配置只在启动时读取；运行中修改 `.env` 后需要重启服务。
- `WEB_AUTH_TOKEN` 首版使用简单 Bearer token 或同等的单用户保护，不实现账号注册。
- session、cookie、token 和下载 URL 中的签名参数不得写入日志或页面。

## 5. 数据和目录设计

### 5.1 SQLite 表

只保留首版展示和同步所需字段：

```text
accounts
  id, x_user_id, username, display_name, status,
  last_sync_at, last_error, created_at, updated_at

posts
  tweet_id PRIMARY KEY, account_id, post_type, text,
  posted_at, permalink, raw_json_path, first_seen_at, updated_at

media
  id, tweet_id, media_type, source_url, local_path,
  download_status, sha256, error, created_at, updated_at

sync_runs
  id, account_id, started_at, finished_at,
  posts_seen, posts_new, media_new, status, error
```

`tweet_id` 是帖子唯一键；重复抓取只更新 `updated_at`、原始 JSON 和可变的展示字段，不创建重复记录。

### 5.2 本地目录

```text
/data/
  archive.sqlite3
  twscrape/
  archive/
    accounts/<username>/posts/<yyyy>/<mm>/<tweet_id>/
      post.json
      media-01.jpg
      media-02.mp4
  logs/
```

媒体先下载到同目录的临时文件，成功后再改名为最终文件名，避免页面读到半个文件。文件名不能直接使用帖子正文，统一使用账号、日期和帖子 ID。

## 6. 同步流程

### 6.1 启动流程

1. 校验环境变量和数据目录权限。
2. 初始化 SQLite 表和必要索引。
3. 初始化 `twscrape` session。
4. 解析 `ARCHIVE_ACCOUNTS`，建立或更新 `accounts` 记录。
5. 启动 Web 服务和后台同步循环。

### 6.2 首次同步

1. 对每个账号读取最多 `ARCHIVE_INITIAL_POST_LIMIT` 条内容；设为 `-1` 时不限制。
2. 将原始响应写入 `post.json`，标准字段写入 SQLite。
3. 对帖子媒体创建下载记录。
4. 调用 `gallery-dl` 下载媒体，并记录路径、哈希和错误。
5. 当前账号完成后写入 `sync_runs`，再处理下一个账号。

### 6.3 增量同步

1. 后台循环按 `ARCHIVE_SYNC_INTERVAL_SECONDS` 触发一次。
2. 每个账号从最新内容开始读取，连续遇到 `ARCHIVE_INCREMENTAL_KNOWN_POST_LIMIT` 条已归档帖子后停止；发现新帖时重新计数，设为 `-1` 时不提前停止。
3. 新帖子先落库，再异步于当前进程中下载媒体。
4. 单条帖子失败不影响同一批次的其他帖子；批次错误写入 `sync_runs` 和 `accounts.last_error`。
5. 下一轮继续重试失败媒体和未完成帖子。

### 6.4 失败处理

- 网络错误和临时限流：指数退避，最多重试 3 次。
- 401/403 或 session 失效：暂停当前账号，页面显示错误，等待人工更新 session。
- 媒体 404：帖子仍标记为已归档，媒体记录标记 `unavailable`。
- 解析错误：保留原始响应路径，避免因为单个异常响应导致进程退出。
- SQLite 写入异常：停止当前同步批次并记录日志，不推进同步状态。

## 7. Web 面板

首版使用 FastAPI + Jinja2 服务端渲染，不单独构建前端工程。

### 7.1 页面

```text
GET /                 账号列表、最近同步状态、统计摘要
GET /accounts/{id}    账号时间线，按时间倒序分页
GET /posts/{tweet_id} 帖子正文、原帖链接、媒体列表、抓取时间
GET /media/{id}       受保护的本地媒体文件
GET /health           存活检查和最近同步状态
```

### 7.2 页面能力

- 账号列表：账号名、归档帖子数、媒体数、最近成功同步时间、错误状态。
- 时间线：正文、发布时间、帖子类型、媒体缩略图和原帖链接。
- 帖子详情：原始 JSON 是否存在、媒体下载状态和本地文件。
- 基础筛选：账号、日期范围、是否包含媒体。
- 分页：按 `posted_at DESC, tweet_id DESC` 稳定排序。

首版页面只读，不提供同步按钮、配置编辑、删除数据和任务重试按钮。同步通过定时器自动执行，故障通过日志和页面状态排查。

## 8. 实施步骤

### Step 1：项目骨架

- 建立 Python 项目和依赖管理。
- 增加配置加载、日志、健康检查。
- 提供 `.env.example`、Dockerfile 和最小 Docker Compose。

完成标准：空数据库可以启动，访问 `/health` 返回正常，缺少必填配置时能清晰失败。

### Step 2：SQLite 存储层

- 创建四张首版表和索引。
- 实现帖子/媒体幂等写入。
- 实现原始 JSON 和媒体目录生成。

完成标准：同一帖子重复写入不会产生重复记录；重启后数据仍可读取。

### Step 3：`twscrape` 抓取适配器

- 封装账号解析、时间线分页和帖子标准化。
- 实现首次历史导入和增量抓取。
- 增加单账号顺序执行和失败记录。

完成标准：配置一个测试账号后可以完成一次同步，并在 SQLite 和 `post.json` 中看到结果。

### Step 4：`gallery-dl` 媒体下载

- 将新增帖子的 URL 转换为媒体下载任务。
- 调用 `gallery-dl` 下载到帖子目录。
- 记录下载状态、文件路径、大小、SHA-256 和错误。

完成标准：图片和视频可以在本地打开；重复同步不会重复下载已完成文件。

### Step 5：只读 Web 面板

- 实现账号列表、时间线、帖子详情、媒体访问和健康页。
- 增加 token 保护和基本分页筛选。
- 为错误账号和失败媒体显示可读状态。

完成标准：未认证请求不能查看归档；认证后可以从账号列表进入帖子详情并打开媒体。

### Step 6：联调与打包

- 测试服务重启、网络中断、媒体下载失败和 session 失效。
- 验证数据目录挂载后容器重建不会丢数据。
- 补充 README：启动命令、环境变量说明和常见故障处理。

完成标准：使用一份 `.env` 和一条 Docker Compose 命令可以启动首版系统。

## 9. 首版验收标准

- 配置多个账号后，系统能按顺序完成首次同步和定时增量同步。
- 帖子元数据、原始 JSON 和可访问媒体均持久化到挂载目录。
- 服务重启不会清空数据，也不会重复创建同一帖子。
- 单个账号或单个媒体失败不会阻塞其他账号和帖子。
- Web 面板可以查看账号、帖子、媒体和同步错误。
- 修改环境变量并重启后，账号列表、频率和目录配置生效。
- 无有效 token 时不能访问账号内容和媒体文件。
- 明确记录：首版不保证发现实时删除，不绕过 X 的访问限制。

## 10. 后续演进顺序

只有首版稳定后再按实际痛点演进：

1. 删除/不可访问状态复查。
2. 手工重试和同步按钮。
3. PostgreSQL 或对象存储。
4. 全文搜索和更丰富的媒体预览。
5. 数据库/媒体异地备份与恢复演练。
6. 将单进程后台循环拆为独立 Worker。
