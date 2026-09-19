# 帖子归档模型 v3 实现及迁移

实现基线：`POST_ARCHIVE_MODEL.md`。业务库使用 SQLite schema v3；twscrape 登录数据库仍独立。

## 模型和运行时

- `x_users` 保存所有已发现作者，`observed_accounts` 只保存主动归档目标。外部作者不会被自动调度。
- `posts` 只接受 original、reply、quote；`reposts` 保存独立事件，通过非空 `origin_tweet_id` 引用内容。自转保留独立事件 ID。
- 两张 observation 表记录归档账号发现的顶层 item；账号统计、时间线和媒体任务都从 observation 查询。
- `media.owner_tweet_id` 只能引用内容帖子，同一 owner/source URL 只有一条媒体记录。repost 和 quote 引用同一原帖时复用媒体及下载任务。
- `post_model.py` 是抓取与迁移共用的解析器。own media 不递归展开；嵌套内容各自保存作者、引用关系和媒体。
- 一个 item 的内容、媒体、事件及 observation 在一个数据库事务内写入。完整内容不会被后续 reference-only 占位覆盖。
- 只有引用 ID、没有作者 ID 时，使用明确的非 X 身份 `x_users.x_user_id = 'unknown'`。它不保存猜测的用户名，不生成数字用户 ID，也不进入归档账号。后续完整快照会补齐真实作者。

文件路径以实体 ID 为准：

```text
archive/posts/<tweet_id>/post.json
archive/posts/<tweet_id>/<media files>
archive/reposts/<repost_tweet_id>/repost.json
```

新内容读取和下载不再经过旧 `accounts/<account>/posts/...` 路径。已删除旧的启动自动迁移、递归媒体摊平和同步时媒体补扫逻辑。历史补全由离线迁移完成。

## API、任务与前端

`GET /api/posts` 在 SQLite 中先 `UNION ALL` 普通帖子和 repost，再按事件时间、tweet ID 排序并分页。无账号筛选时，一个实体不会因多个观察关系重复返回；仅作为引用发现的内容不单独混入时间线。

规范查询参数是 `observed_account_x_user_id`；旧 `account_x_user_id` 仅在 HTTP 入口映射。两个值冲突返回 422。默认搜索外层正文；显式 `search_origin=true` 才匹配 repost 的原文。

API 返回结构化 `author`、`reference`、`origin`、`reposter`。repost 的链接和时间仍属于事件；`metrics_source_tweet_id` 明确互动数据属于原帖。前端只渲染一次 origin，quote 的正文与引用卡片分别渲染各自媒体，事件与原帖链接分开。

`has_media` 只检查自身（repost 则检查 origin）及可见的 quote 引用链；不会把 reply 目标的图片算作回复的媒体。回复目标缺少用户 ID 时，`reply_to_username` 回退到该帖子快照中的 `inReplyToScreenName`/`inReplyToUser.username`，仅用于展示，不写入共享的 `unknown` 用户；已知目标作者的当前用户名优先。

任务表及同步记录使用 `observed_account_x_user_id`。媒体上下文包含 canonical media ID、owner tweet ID、内容作者和可选的归档账号；优先从父同步任务确定观察账号，否则从 observation 查找。外部作者没有 observed account 也可以下载及重试。

同步统计：`posts_seen` 是顶层 item 数，`posts_new` 是新增 observation 数，`referenced_new` 是新建嵌套内容数，`reposts_new` 是新增转发事件数，`media_new` 是新增媒体记录数。旧同步历史原计数保留，新增两项为 0。

## 离线迁移

以下为完整备份模式。归档盘无法容纳第二份媒体时，使用
[rc3 发布说明中的低空间迁移流程](RELEASE_V0.1.2_RC3.md)，不要运行下述整目录复制。

先由操作者停止 API、crawl/media worker、scheduler，并排空 Redis 投递。命令不会启动、终止这些进程，也不修改 Redis；`--apply` 和 `--resume-from` 在切换前检查活动任务，源库或候选库有 queued/in_progress/retry_scheduled 任务时拒绝切换。应用启动发现旧 schema 会要求先迁移。

默认只演练，源数据库与文件不变：

```sh
.venv/bin/python -m archivex.migrate \
  --database data/archive.sqlite3 \
  --archive-dir data/archive \
  --session-path data/twscrape \
  --output-dir backups/post-model-v3-review
```

输出目录必须尚不存在，包含：

```text
backup/archive.sqlite3          SQLite backup API 一致快照
backup/archive/                完整旧归档文件
backup/twscrape/accounts.db     登录库快照（源文件存在时）
backup/manifest.json            源路径、数据库和每个归档文件的 SHA-256
candidate/archive.sqlite3      完整 v3 候选库
candidate/archive/             规范路径的 JSON 与媒体
report.json                    逐条顶层映射、旧/新媒体 ID 映射、问题和校验结果
```

账号启停、用户名历史、同步记录、任务 ID、父子/重试关系和所有 attempts 都保留。相同 owner/URL 的旧媒体优先选用通过哈希校验的已完成文件；旧任务的 `media_id`、执行 args/kwargs/labels 和当前内容上下文会映射到保留的 ID。原上下文保存在 `migration_context`，历史 attempts/result/error 不被改写。

旧顶层记录按 ID 一一映射，不用新 posts 总数与旧帖子数比较。无法解析 origin、无法确定媒体归属、文件缺失、JSON 损坏或哈希不符时，问题同时写入报告和候选库 `archive_migration_errors`，禁止应用；正式 `reposts.origin_tweet_id` 不放宽为 nullable。未入库文件、异常文件及不同内容的备用下载保存在候选归档 `preserved/`，不会静默丢弃。

检查报告后应用同一个候选结果：

```sh
.venv/bin/python -m archivex.migrate --resume-from backups/post-model-v3-review
```

也可用原命令加 `--apply`，在创建备份、构建与校验候选后直接应用。安装前检查报告已完成构建、候选库有本次迁移的成功记录，且重新校验的计数和文件检查结果与报告一致；同时确认源库及每个源文件没有变化。文件先写入目标目录内带迁移 ID 的临时文件，刷新到磁盘并校验 SHA-256 后原子替换到最终路径，再通过 SQLite backup API 事务式替换数据库。确认所有数据库路径、媒体哈希、完整性及外键无误后，仅删除已经完整备份且字节未变的旧文件，清理空目录。备份保留在输出目录，运行库不保留旧表、别名或 staging 表。

`--keep-legacy-files` 可暂缓清理；后续单独执行：

```sh
.venv/bin/python -m archivex.migrate \
  --database data/archive.sqlite3 --archive-dir data/archive \
  --cleanup-from backups/post-model-v3-review
```

安装中断时用 `--resume-from` 继续；恢复和回滚仅清理可确定属于本次安装的临时文件，其他新增或被改写的文件仍会阻止操作。若失败发生在候选库构建阶段（报告为 `building`、缺少校验结果或候选迁移记录尚未成功），不能使用恢复入口安装残缺数据；应修复错误后使用新的输出目录重新演练。已经完成的 v3 数据库再次执行迁移只做校验，不重复创建数据。

回滚前保持服务停止，使用：

```sh
.venv/bin/python -m archivex.migrate --rollback-from backups/post-model-v3-review
```

回滚恢复该次迁移的 v2 数据库和全部旧文件，并移除该次新增的规范文件；随后需使用旧版本应用。若数据库或归档已有新写入，会拒绝回滚以保护新数据。twscrape 登录库在迁移和回滚中均不修改，其快照用于独立恢复。

## 验证

后端测试覆盖外部作者、共享 origin、自转、reply 占位补齐、quote 媒体分层、原文搜索、混合分页、任务重试、真实 v2 schema 迁移、旧用户名路径、终态任务/attempts 保留、缺失数据阻止应用、中断续迁和回滚保护。前端执行 TypeScript 检查及 Vite 生产构建，不启动 dev 进程。

审查修复增加了构建中途失败拒绝恢复、三种活动任务状态拒绝恢复、复制异常及子进程强制退出后的续迁/回滚、保留无关临时文件、reply/quote/repost 媒体筛选，以及回复用户名回退和 `unknown` 身份隔离的回归测试。

## 本地数据执行记录（2026-09-19）

已对本地 `data/archive.sqlite3` 和 `data/archive` 执行最终迁移；没有启动或终止应用进程。

- 完整迁移前备份：`backups/pre-post-archive-model-20260919T150000Z.tar.gz`，使用现有备份工具创建并验证。
- 最终迁移输出：`backups/post-model-v3-20260919/`；报告状态 `applied_and_cleaned`，未解决问题 0。
- 原库 3 个归档账号保持不变；新库共 491 条用户身份。
- 原库 3565 条顶层记录对应 3533 条 post observation、32 条 repost observation。
- 新库 6434 条内容记录包含引用内容及占位；32 条 repost 独立保存。
- 媒体记录 273 → 247；26 条重复 owner/source URL 记录归并，完成的媒体文件均复用并校验 SHA-256。
- 851 条同步记录保留。实际迁移时任务/attempt 表为空；非空任务迁移、ID 重映射与重试由测试验证。
- 3818 个数据库引用文件通过路径、实体 ID 和媒体哈希校验；`integrity_check=ok`、`foreign_key_check=[]`。
- 旧 `data/archive/accounts/` 已清理，全部原始文件仍在备份；未入库文件保存在 `preserved/`。

验证结果：154 个后端测试通过；TypeScript 检查和 Vite 生产构建通过；迁移、模型与存储专项 24 个测试在最终校验补强后通过。未启动服务做线上抓取测试。
