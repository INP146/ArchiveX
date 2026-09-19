# ArchiveX 帖子归档模型与实现说明

- 文档日期：2026-09-19
- 文档状态：实现基线
- 适用范围：帖子、引用关系、repost、外部作者、媒体归属、归档账号观察关系
- 目标读者：后端、数据迁移、任务队列和前端开发者

本文档是帖子关系模型重构的实现依据。实现时以当前代码为事实来源，不以早期
`.agents/ARCHITECTURE.md` 中的 PostgreSQL、MinIO 和服务拆分方案为事实来源。当前
运行系统使用 SQLite 保存 ArchiveX 业务数据，使用 Redis/Taskiq 处理后台任务，
使用独立的 twscrape `accounts.db` 保存抓取登录态。

其他以 `accounts` 为业务主表的旧文档（例如 `ACCOUNT_IDENTITY.md`、
`DATABASE_CONSOLIDATION.md`）描述的是当前 v2/历史状态；涉及 v3 目标表名和关系时，以本文档为准。

## 1. 结论先行

最终采用以下模型，不再在“所有内容放一张表”和“所有发现对象都绑定归档账号”之间摇摆：

```text
x_users
    X 上所有被系统见过的用户身份，包括外部原帖作者

observed_accounts
    用户主动要求持续归档的账号；它是 x_users 的子集

posts
    有自身内容的帖子：original、quote、reply，以及被引用的外部原帖

reposts
    独立的 repost 事件；不复制原帖正文和媒体

archive_post_observations
    哪个归档账号的时间线发现了哪个 posts

archive_repost_observations
    哪个归档账号的时间线发现了哪个 reposts

media
    归属于真正拥有媒体的 posts；不归属于 repost
```

核心规则：

1. `observed_accounts` 只表示主动归档目标，不表示系统见过的所有 X 用户。
2. `x_users` 保存所有被发现过的作者身份，外部作者也可以只有一条 `x_users` 记录。
3. `posts` 保存原帖、quote、reply 和被引用的外部原帖。
4. quote 与普通帖共用 `posts`，quote 通过 `reference_tweet_id` 指向被引用原帖。
5. repost 单独存入 `reposts`，至少保存自己的 tweet ID、origin、转发者、时间和链接。
6. repost 的正文、作者展示内容和媒体通过 origin 读取，不复制到 repost 记录。
7. 自转自帖仍然是独立 repost 事件；不能因为作者相同而合并。
8. 外部原帖可以进入 `posts`，但在其作者没有被主动归档前，不会进入主动同步范围。
9. 媒体按内容所有者归属；repost 只引用原帖媒体。
10. “作者是谁”和“哪个归档账号发现了它”必须由不同字段/关系表达。

## 2. 当前系统的真实实现

### 2.1 存储和运行时

当前 ArchiveX 业务数据库是 SQLite，schema 版本在
`src/archivex/storage.py` 中为 `SCHEMA_VERSION = 2`。业务表和任务生命周期表共用
`ARCHIVE_DB_PATH`：

```text
accounts
account_username_history
posts
media
sync_runs
queue_tasks
queue_attempts
```

twscrape 的登录账号、请求锁和抓取状态继续保存在独立的 `twscrape/accounts.db`，
不属于 ArchiveX 的业务 schema。

相关代码：

- 业务 schema 和仓储：`src/archivex/storage.py`
- 抓取解析：`src/archivex/source.py`
- 同步流程：`src/archivex/sync.py`
- Taskiq 任务：`src/archivex/tasks.py`
- 任务中心 schema 和关联上下文：`src/archivex/task_center.py`
- API：`src/archivex/api.py`
- 时间线前端：`frontend/src/features/timeline/post-timeline.tsx`

### 2.2 当前 `accounts`

当前 `accounts` 的真实语义是“主动归档账号”：

```text
x_user_id
current_username
display_name
archive_enabled
status
last_sync_at
last_error
created_at
updated_at
```

`list_enabled_account_ids()` 只返回 `archive_enabled = 1` 的账号，scheduler 只为这些
账号创建同步任务。账号添加、暂停、恢复、用户名历史和同步运行也都以这个表为中心。

因此开发者不应把当前 `accounts` 理解为“所有曾经出现在 payload 中的 X 用户”。

### 2.3 当前 `posts`

当前 schema 是：

```sql
CREATE TABLE posts (
    tweet_id TEXT PRIMARY KEY,
    account_x_user_id TEXT NOT NULL REFERENCES accounts(x_user_id),
    post_type TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT NOT NULL,
    media_scanned_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

这造成了当前模型的根本限制：一条帖子必须绑定到已归档账号，因而无法自然保存
“已归档账号 A 转发了未归档账号 B 的原帖”中的 B 的原帖。

当前帖子入库由 `ArchiveSyncService` 调用 `ArchiveRepository.upsert_post()` 完成，
以 `tweet_id` 幂等。相同 tweet ID 如果被认为属于不同账号会直接报错，而不是建立
多条观察关系。

当前原始 JSON 路径按归档账号组织：

```text
accounts/<account_x_user_id>/posts/<YYYY>/<MM>/<tweet_id>/post.json
```

这个路径假设帖子属于一个归档账号，在共享外部原帖的模型下需要调整。

### 2.4 当前抓取和帖子类型判断

`TwscrapePostSource.fetch_timeline()` 调用 `user_tweets_and_replies()`，并过滤：

```python
if str(tweet.user.id) != x_user_id:
    continue
```

因此只有被同步账号作为顶层作者/行为主体的帖子会产生 `SourcePost`。嵌入在
`retweetedTweet` 或 `quotedTweet` 中的其他用户帖子不会自动单独发出一条
`SourcePost`。

当前类型判断优先级是：

```python
if tweet.retweetedTweet is not None:
    repost
elif tweet.quotedTweet is not None or tweet.isQuoteStatus:
    quote
elif tweet.inReplyToTweetId is not None:
    reply
else:
    original
```

这意味着：如果 payload 同时出现多个嵌套字段，`repost` 优先于 `quote`，`quote`
优先于 `reply`。重构时必须保留这个优先级，除非同步修改测试和产品语义。

### 2.5 当前媒体处理

`media_from_payload()` 会递归提取：

```text
当前 tweet.media
当前 tweet.retweetedTweet.media
当前 tweet.quotedTweet.media
```

它只在同一个 payload 内按 URL 去重。随后同步服务把所有提取结果都以外层
`tweet_id` 写入 `media`：

```python
MediaInput(tweet_id, media.media_type, media.source_url)
```

因此当前行为是：

- repost 的原帖媒体挂在 repost 的 tweet ID 下；
- quote 自己的媒体和被引用原帖的媒体混在 quote 的 tweet ID 下；
- 多个 repost 会产生多组媒体记录和下载任务；
- `UNIQUE(tweet_id, source_url)` 只能防止同一帖子内重复，不能防止跨 repost 重复。

这是本次模型重构必须修正的主要重复来源。

### 2.6 当前展示和 API

当前 API `/api/posts` 直接按 `posts.account_x_user_id` 查询，支持：

```text
account_x_user_id
q
from / to
has_media
post_type
exclude_post_type
limit / offset
```

`_post_response()` 再从 raw JSON 读取 metrics、presentation 和媒体。

当前 `post_presentation()` 只对 `retweetedTweet` 做特殊处理：

- repost 展示嵌套原作者和原文，并返回 `reposted_by_display_name`；
- quote 没有结构化的 `quoted_post` 返回对象；
- quote 默认仍按外层 payload 展示；
- 媒体仍来自外层 tweet ID 下的混合媒体记录。

前端 `PostItem` 只通过 `reposted_by_display_name` 显示“某某已转帖”，没有独立的
quote 卡片模型，也没有读取 `origin` 或 `reference` 关系。

## 3. 目标领域模型

### 3.1 `x_users`：所有被发现的 X 用户

这是身份表，不是归档策略表：

```sql
CREATE TABLE x_users (
    x_user_id TEXT PRIMARY KEY,
    current_username TEXT,
    display_name TEXT,
    profile_image_url TEXT,
    description TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

写入来源包括：

- `resolve_account()` 返回的账号；
- 已归档账号时间线中的顶层作者；
- repost/quote/reply 的嵌入作者；
- 以后单独抓取的帖子详情。

外部作者可以只有 `x_user_id`，其他字段允许为空。用户名变化不改变主键。

目标 v3 的第一实现阶段只为 `observed_accounts` 维护完整的用户名历史；外部作者先保存
当前快照，不为了本次重构引入额外的外部账号同步流程。

### 3.2 `observed_accounts`：主动归档账号

目标 schema 直接使用 `observed_accounts`。`accounts` 只作为当前 v2 数据库的历史表名，
迁移完成后不再作为目标模型的物理表名保留：

这里不存在“第一阶段逻辑上叫 `observed_accounts`、物理上仍叫 `accounts`”的过渡设计。
v3 第一实现阶段就使用 `observed_accounts`；`accounts` 只会出现在 v2 读取、迁移
脚本和历史代码说明中。

```sql
CREATE TABLE observed_accounts (
    x_user_id TEXT PRIMARY KEY REFERENCES x_users(x_user_id),
    archive_enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`current_username`、`display_name` 的规范来源应逐步移到 `x_users`。为了降低一次迁移
风险，v2 迁移脚本可以先读取旧 `accounts` 上的冗余列，但目标表不再保留这套命名；所有新写入必须同步更新
`x_users`，查询优先使用 `x_users`。

用户名历史是 `observed_accounts` 的辅助关系，目标物理表名统一为
`observed_account_username_history`，并以 `x_user_id` 外键指向 `observed_accounts`。旧的
`account_username_history` 只作为 v2 迁移来源，不作为 v3 的同义别名。

目标 v3 的其他归档账号关联也统一使用同一命名：

```text
observed_account_username_history.x_user_id -> observed_accounts.x_user_id
sync_runs.observed_account_x_user_id         -> observed_accounts.x_user_id
queue_tasks.observed_account_x_user_id       -> observed_accounts.x_user_id
```

### 3.3 `posts`：有自身内容的帖子

目标定义：

```sql
CREATE TABLE posts (
    tweet_id TEXT PRIMARY KEY,
    author_x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
    post_type TEXT NOT NULL CHECK (post_type IN ('original', 'reply', 'quote')),
    reference_tweet_id TEXT REFERENCES posts(tweet_id),
    text TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT,
    capture_source TEXT NOT NULL DEFAULT 'timeline',
    availability TEXT NOT NULL DEFAULT 'available',
    media_scanned_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

字段语义：

- `author_x_user_id`：真实发帖作者，不是发现它的归档账号。
- `post_type=original`：没有引用/回复目标。
- `post_type=quote`：自身有正文，`reference_tweet_id` 指向被引用原帖。
- `post_type=reply`：自身有正文，`reference_tweet_id` 指向被回复帖子。
- `text`：只保存该帖子自己的正文，不把引用原文拼进去。
- `capture_source=timeline`：直接从某个归档账号顶层时间线发现。
- `capture_source=embedded_reference`：只因 repost/quote/reply 嵌套而发现。
- `availability`：允许 `available`、`partial`、`deleted_or_unavailable`、`unknown`。
- `raw_json_path`：外部引用只有嵌套快照时也可以为空；原始嵌套对象应由所属外层
  raw payload 保留。

如果引用目标只提供了 ID，没有完整对象，则先创建 reference-only 的占位 `posts`：

```text
text = ''
availability = 'unknown'
capture_source = 'embedded_reference'
raw_json_path = NULL
```

这样关系可以使用外键，后续详情抓取成功时再补齐内容。

### 3.4 `reposts`：独立的转帖事件

目标定义：

```sql
CREATE TABLE reposts (
    repost_tweet_id TEXT PRIMARY KEY,
    origin_tweet_id TEXT NOT NULL REFERENCES posts(tweet_id),
    reposter_x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
    reposted_at TEXT NOT NULL,
    permalink TEXT NOT NULL,
    raw_json_path TEXT,
    availability TEXT NOT NULL DEFAULT 'available',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

repost 必须保存的只是事件属性：

```text
repost_tweet_id
origin_tweet_id
reposter_x_user_id
reposted_at
permalink
raw_json_path（建议保留）
```

不在 `reposts` 中复制：

```text
原帖作者
原帖正文
原帖媒体
```

这些从 `origin_tweet_id` 对应的 `posts` 读取。

`reposts.origin_tweet_id` 在最终 v3 表中保持 `NOT NULL`。如果历史坏数据无法解析
origin，迁移程序应先写入迁移错误报告/暂存表，待人工或详情任务补齐后再写入正式的
`reposts`；不能通过把正式表的外键临时改成可空来掩盖问题，也不能伪造 tweet ID。

### 3.5 归档观察关系

因为 `posts.author_x_user_id` 和归档账号不是同一概念，所以不能再把帖子作者直接
放进旧模型的 `posts.account_x_user_id`。

有自身内容的帖子观察关系：

```sql
CREATE TABLE archive_post_observations (
    observed_account_x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (observed_account_x_user_id, tweet_id)
);
```

repost 观察关系：

```sql
CREATE TABLE archive_repost_observations (
    observed_account_x_user_id TEXT NOT NULL REFERENCES observed_accounts(x_user_id),
    repost_tweet_id TEXT NOT NULL REFERENCES reposts(repost_tweet_id) ON DELETE CASCADE,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (observed_account_x_user_id, repost_tweet_id)
);
```

语义示例：

```text
user_a 是主动归档账号
user_b 不是主动归档账号

posts:
    100 = user_b 的原帖

reposts:
    200 = user_a 转发 100

archive_repost_observations:
    user_a -> 200
```

此时不会生成 `user_a -> 100` 的 `archive_post_observations`，因为 user_a 发现的是
repost 事件，不是以自己的顶层帖子身份发现了原帖 100。

以后如果 user_b 被加入 `observed_accounts`，并且同步 user_b 的时间线，才新增：

```text
archive_post_observations:
    user_b -> 100
```

原帖 100 和其媒体不重复创建。

### 3.6 `media`：归属于内容帖子

v3 继续使用物理表名 `media`；但媒体关联列统一改为表示“媒体拥有者”的
`owner_tweet_id`：

```sql
CREATE TABLE media (
    id TEXT PRIMARY KEY,
    owner_tweet_id TEXT NOT NULL REFERENCES posts(tweet_id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_tweet_id, source_url)
);
```

v3 第一实现阶段不要求引入独立的 `media_assets` 与 `post_media` 两级模型；但实现时应保留
以后拆分的可能性。`owner_tweet_id` 不能指向 `reposts`。

媒体归属规则：

| 场景 | 媒体拥有者 |
| --- | --- |
| 普通帖自己的媒体 | 普通帖 `posts.tweet_id` |
| quote 自己附带的媒体 | quote `posts.tweet_id` |
| quote 被引用原帖的媒体 | 被引用原帖 `posts.tweet_id` |
| repost 中展示的媒体 | 原帖 `posts.tweet_id` |
| 自转自帖中的媒体 | 原帖 `posts.tweet_id` |

## 4. 各类帖子行为定义

### 4.1 Original

```text
posts(tweet_id=T1, post_type=original, reference_tweet_id=NULL)
    archive_post_observations(observed_account_x_user_id=A, tweet_id=T1)
```

媒体挂在 T1。

### 4.2 Quote

quote 是一个有自身正文的帖子，和 original 共用 `posts`：

```text
posts(T1, original, reference=NULL, text="原文")
posts(T2, quote, reference=T1, text="我的评论")
archive_post_observations(observed_account_x_user_id=A, tweet_id=T2)
```

quote 自己的正文不能被原帖覆盖。API 应同时返回 `reference`/`quoted_post`，前端应
渲染外层 quote 和被引用内容，而不是从 raw JSON 临时猜测。

如果引用目标不在归档范围，仍然创建 T1 的 `posts` 占位/内容行，但不创建对 A 的
原帖观察关系。

### 4.3 Reply

reply 同样有自己的正文：

```text
posts(T2, reply, reference=T1, text="回复内容")
archive_post_observations(observed_account_x_user_id=A, tweet_id=T2)
```

回复目标可以是外部账号的帖子。目标只有 ID 时创建 reference-only 占位行。

### 4.4 Repost

```text
posts(T1, original, ...)
reposts(R1, origin=T1, reposter_x_user_id=A, ...)
archive_repost_observations(observed_account_x_user_id=A, repost_tweet_id=R1)
```

repost 自己没有独立正文和媒体。它的时间线位置使用 `reposted_at`，展示内容使用
origin T1。

### 4.5 自转自帖

自转只是一种作者相同的 repost：

```text
posts(T1, author_x_user_id=A, original)
reposts(R1, origin=T1, reposter_x_user_id=A)
```

不能因为 `reposter_x_user_id == origin.author_x_user_id` 就合并或转换成 original。
R1 仍有独立的 tweet ID、时间、链接和事件意义。

## 5. 抓取入库流程

### 5.1 Source 层改造

当前 `SourcePost` 只有扁平的 `media`，不能表达媒体所有权和嵌套关系。应改为能够
表达以下结构的解析结果，名称可按代码风格调整：

```python
ParsedTimelineItem(
    tweet_id="...",
    author=UserSnapshot(...),
    kind="original | reply | quote | repost",
    text="outer post's own text",
    posted_at=...,                  # outer event time
    permalink="...",
    own_media=(...),                # only outer media
    referenced=ParsedReferencedItem(
        tweet_id="...",
        author=UserSnapshot(...),
        text="...",
        posted_at=...,
        permalink="...",
        media=(...),
        raw_payload={...},
    ),
    raw_payload={...},
)
```

不要继续让 `media_from_payload()` 把外层和所有嵌套媒体合并成一个元组；媒体所有权
必须在解析阶段保留。

### 5.2 统一 upsert 顺序

对每条归档账号时间线返回的顶层 item：

1. `ensure_x_user()` 写入顶层作者和所有嵌套作者的身份快照。
2. 如果是 original/reply/quote：
   - upsert 外层 `posts`；
   - 对 reply/quote 确保 reference 目标 `posts` 存在；
   - 写入/更新 `archive_post_observations`。
3. 如果是 repost：
   - 确保 origin `posts` 存在，必要时创建 `embedded_reference` 占位/内容行；
   - upsert `reposts`；
   - 写入/更新 `archive_repost_observations`。
4. 只为每个内容帖子写入它自己拥有的媒体。
5. 由媒体队列按 `media.id` 下载，不能按每个 repost 再创建一份下载任务。
6. 最后更新同步运行计数和账号游标/时间。

整个顶层 item 的数据库写入必须幂等。重复抓取同一个 repost 不应重复创建
`reposts` 或 observation；重复发现同一个 origin 不应重复创建 `posts` 或媒体。

### 5.3 账户身份和外部身份

同步归档账号时：

- 外层作者必须写入 `x_users`；
- 归档账号的用户名历史写入目标 `observed_account_username_history`；v2 迁移前仍读取旧的
  `account_username_history`；
- 嵌套外部作者至少写入 `x_users` 的当前快照；
- 不为外部作者创建同步任务；
- 不把外部作者加入 `observed_accounts`，除非用户明确添加该账号。

### 5.4 发现和归档的计数

当前 `sync_runs.posts_seen` 表示从顶层时间线拿到的 `SourcePost` 数量。重构后应
明确计数含义：

```text
posts_seen       顶层时间线 item 数量（包括 repost）
posts_new        新建 archive observation 的数量，或兼容期继续沿用旧定义
referenced_new   本次新建的外部 origin/reference posts 数量
reposts_new      本次新建的 repost 事件数量
media_new        新建媒体关联数量
```

如果暂时不扩展 `sync_runs` 字段，至少在代码注释和任务结果中说明 `posts_new` 不等于
新建的所有 `posts` 行，因为外部原帖可能是嵌套发现的。

## 6. 原始文件路径

当前路径绑定归档账号，目标模型应改为按内容实体绑定：

```text
archive/posts/<tweet_id>/post.json
archive/posts/<tweet_id>/<media files>
archive/reposts/<repost_tweet_id>/repost.json
```

理由：

- 原帖可能被多个归档账号引用；
- 原帖可能先作为外部引用发现，后来作者被主动归档；
- 内容和媒体只能有一个规范目录；
- repost 的 raw payload 与原帖 raw payload 是两种不同快照，应分开保存。

`post_directory()` 必须按内容帖子 ID工作；repost 原始 JSON 使用单独的
`repost_directory()` 或统一的 archive item 目录函数。

迁移期间不得直接删除旧目录。应先完整备份数据库和 archive 数据目录，复制/移动后
校验：

```text
数据库中的 raw_json_path 存在
媒体 local_path 存在且仍指向正确文件
PRAGMA foreign_key_check 无错误
旧数据库中的每个顶层 item 都能一一映射到新模型中的一个 `posts` 或 `reposts` item；
新 `posts` 行数可以因为外部 origin 增加，不能直接与旧 `posts` 行数比较
```

校验成功后再清理旧的 `accounts/<account>/posts/...` 目录；这里的 `accounts` 是历史
文件目录名，不是 v3 的业务表名。

## 7. Repository 层改造

### 7.1 保留的能力

以下能力应继续保留：

- 以稳定 X ID 幂等；
- 原始 JSON 原子写入；
- 失败媒体重试；
- `sync_runs` 中断收尾；
- 账号启停和用户名历史；
- 任务表和 Archive DB 共用 SQLite；
- `twscrape/accounts.db` 独立。

### 7.2 新增/替换的仓储方法

建议提供明确的方法，不让业务层直接拼复杂 SQL：

```python
ensure_x_user(snapshot) -> XUser
upsert_post(post: PostInput) -> UpsertResult
upsert_repost(repost: RepostInput) -> UpsertResult
ensure_reference_post(reference: ReferencedPostInput) -> Post
observe_post(observed_account_x_user_id, tweet_id, observed_at)
observe_repost(observed_account_x_user_id, repost_tweet_id, observed_at)
upsert_post_media(owner_tweet_id, media)
get_timeline_items(observed_account_x_user_id, filters...) -> list[TimelineItem]
get_timeline_item(tweet_id) -> TimelineItem | None
```

`upsert_post()` 不再接收 `observed_account_x_user_id` 作为作者归属；如果需要记录发现来源，
由 `observe_post()` 单独完成。

### 7.3 账号统计

当前 v2 的 `list_accounts()` 通过 `COUNT(posts.tweet_id)` 统计账号帖子数。目标模型下不能
再按 `posts.author_x_user_id` 直接计数，应该统计：

- `archive_post_observations` 中该账号的数量；
- `archive_repost_observations` 中该账号的数量；
- 或按产品定义只统计顶层 timeline item 总数。

必须避免把外部 origin 的数量误算到主动归档账号。

## 8. API 合同

### 8.1 查询接口

保留现有端点路径，内部实现改为查询 observation；v3 的规范参数名是
`observed_account_x_user_id`，旧参数仅作为兼容别名：

```text
GET /api/posts?observed_account_x_user_id=...
GET /api/posts?account_x_user_id=...       # v2 兼容别名
GET /api/posts/{tweet_id}
```

两个参数都表示“查看哪个归档账号的时间线”，不是帖子作者；收到旧参数后必须立即
映射为 `observed_account_x_user_id`，不能把旧参数继续传入 v3 repository。

查询结果应是统一的 timeline item，而不是要求前端再次解析 raw JSON。建议返回形状：

```json
{
  "item_type": "post",
  "tweet_id": "200",
  "observed_account_x_user_id": "42",
  "post_type": "quote",
  "author": {
    "x_user_id": "99",
    "username": "external_user",
    "display_name": "External User"
  },
  "text": "外层 quote 自己的正文",
  "posted_at": "2026-09-19T10:00:00+00:00",
  "permalink": "https://x.com/.../status/200",
  "reference": {
    "tweet_id": "100",
    "author": {"x_user_id": "88", "username": "origin_user"},
    "text": "被引用的原帖",
    "media": []
  },
  "media": []
}
```

repost 返回：

```json
{
  "item_type": "repost",
  "tweet_id": "300",
  "observed_account_x_user_id": "42",
  "post_type": "repost",
  "reposter": {"x_user_id": "42", "username": "archived_user"},
  "reposted_at": "2026-09-19T11:00:00+00:00",
  "permalink": "https://x.com/.../status/300",
  "origin": {
    "tweet_id": "100",
    "author": {"x_user_id": "88", "username": "origin_user"},
    "text": "原帖",
    "media": []
  }
}
```

兼容期可以继续提供当前扁平字段（例如 `display_text`、`author_username`、
`reposted_by_display_name`），但新代码不能依赖 raw payload 自己拼出引用关系。

返回对象中的 `post_type: "repost"` 是 API/筛选层的兼容表示；它不是 `posts.post_type`
的值。正式存储仍然来自 `reposts` 表。

### 8.2 详情、metrics 和可用性

`GET /api/posts/{tweet_id}` 需要同时支持普通 `posts` 和 `reposts`。如果两个表都以
全局 X tweet ID 为主键，仓储层可以先查 reposts 再查 posts，或者建立统一的
timeline item 访问器。

metrics 必须明确对象：

- 原帖 metrics 从 origin payload/快照读取；
- quote metrics 从 quote 自己的 payload 读取；
- repost 页面默认展示 origin 内容，但事件链接和时间属于 repost；
- 不要把 repost 事件 metrics 和 origin metrics 静默混合。

### 8.3 搜索和分页

账号时间线分页必须基于 observation 的事件时间：

- 普通/quote/reply 使用 `posts.posted_at`；
- repost 使用 `reposts.reposted_at`。

不能先分别查两张表再在 Python 中简单拼接，否则 offset 分页会重复或漏项。应在
SQLite 中使用 `UNION ALL` 形成统一的 timeline item 查询，再按事件时间和 tweet ID
排序。

搜索默认搜索外层 item 自己的正文；是否搜索 origin 正文应作为明确的过滤选项，不能
让 repost 的 origin 文本自动导致多个事件重复命中而改变分页数量。

## 9. 前端改造要求

前端不能再把 `ArchivedPost` 假设成“每条记录都是 posts 表行”。建议改成：

```text
TimelineItem
    item_type: post | repost
    tweet_id
    observed_account_x_user_id
    post: PostContent
    origin?: PostContent
    repost?: RepostEvent
```

展示规则：

- original/reply/quote 展示外层 `post`；
- quote 额外展示 `reference` 内容卡片；
- repost 展示“某账号已转帖”标识，再展示 `origin`；
- repost 媒体来自 origin；
- quote 的外层媒体和 reference 媒体分开渲染；
- 用户点击 repost 链接时打开 repost 自己的 permalink，点击 origin 时打开原帖链接。

前端的去重 Map 仍可按 `item_type + tweet_id`，但不能只按展示文本去重。

## 10. Taskiq 和任务中心影响

目标 schema 中，账号同步任务以 `observed_accounts.x_user_id` 为目标，不改变任务名称：

```text
archivex.sync_account
archivex.download_media
archivex.schedule_enabled_accounts
```

需要修改的地方：

1. v2 的 `queue_tasks.account_x_user_id` 在迁移兼容期间继续表示任务目标归档账号；v3
   必须改为 `queue_tasks.observed_account_x_user_id`，并引用 `observed_accounts(x_user_id)`，
   不能改成帖子作者。
2. 媒体任务的上下文不能再强制通过
   `media -> posts -> observed_accounts` 找到账号，因为外部 origin 可能没有归档账号。
3. 任务上下文至少保存 `media.owner_tweet_id`、`post` 作者和可选的父账号任务。
4. `_enqueue_account_media()` 不能继续按旧的 `posts.account_x_user_id` 找媒体，应按该账号
   的 observation 找到其产生/发现的内容媒体，并通过 `media.id` 去重。
5. 一个 origin 只允许一个媒体下载任务；多个 repost 不能为同一个 source URL 重新
   入队。
6. 任务中心展示账号信息时优先使用明确的父账号/observation，不要因为外部作者不在
   `observed_accounts` 而把媒体任务判定为不存在。

相关当前查询集中在 `src/archivex/task_center.py` 的 `_task_metadata()`，当前 v2 仍有
`media JOIN posts JOIN accounts` 这种 v2 上下文构造；迁移后需要改为目标关系，并允许
外部 origin 没有 `observed_accounts` 行。

## 11. 数据迁移方案

本次变更不是简单加列，属于 schema v3 级别的关系重构。迁移前必须停止 API、crawl
worker、media worker 和 scheduler，并备份：

```text
archive.sqlite3
archive 数据目录
twscrape/accounts.db（按现有备份流程）
```

### 11.1 迁移顺序

1. 创建 `x_users`，把现有 `accounts.x_user_id` 导入为 x_users。
2. 将 v2 的 `accounts` 数据迁移到目标 `observed_accounts`，并更新所有目标外键；
   不保留一个同时叫 `accounts` 的目标别名。
3. 创建 v3 的 `posts`、`reposts`、观察关系、`media`、用户名历史和同步/任务关联表；
   迁移用的 staging 表使用明确的临时名称，不能把 staging 表当成最终 schema。
4. 遍历旧 `posts`：
   - `original/reply/quote` 迁移为新的 `posts`；
   - `repost` 解析 raw JSON 的 `retweetedTweet`，创建 origin `posts` 和 `reposts`；
   - 无法解析 origin ID 的历史行标记为 unresolved，不能伪造 ID。
5. 从 raw JSON 补齐嵌套作者到 `x_users`。
6. 每一条旧的普通/quote/reply 帖子创建 `archive_post_observations`。
7. 每一条旧的 repost 创建 `archive_repost_observations`。
8. 迁移媒体：
   - 普通帖媒体保留原 owner；
   - repost 媒体改挂到 origin；
   - quote 根据 raw JSON 中媒体所在层级分别挂到外层 quote 或引用原帖；
   - 无法判断归属的媒体进入明确的迁移错误报告，不要静默丢弃。
9. 将 `account_username_history` 迁移为 `observed_account_username_history`；将
   `sync_runs.account_x_user_id` 和 `queue_tasks.account_x_user_id` 迁移为
   `observed_account_x_user_id`，并分别引用 `observed_accounts(x_user_id)`。
10. 迁移 raw JSON 路径到内容/事件目录，或者先保留旧路径并写入新的实体路径；两者
   必须在文档和代码中明确，不能让路径语义混用。
11. 运行完整性和业务校验后，切换 schema version。

### 11.2 迁移校验清单

```text
PRAGMA integrity_check = ok
PRAGMA foreign_key_check 无结果
旧数据库中的每个顶层 item 都能一一映射到新模型中的一个 `posts` 或 `reposts` item
（新 `posts` 行数可以因为外部 origin 增加，不能直接与旧 `posts` 行数比较）
旧 repost 都有 origin 或明确 unresolved 状态
quote 的外层正文没有丢失
repost 原帖正文没有重复写入事件表
同一 origin 被多个 repost 引用时只有一份 origin content
同一 origin 的媒体没有按 repost 数量倍增
现有 queue_tasks / queue_attempts 仍能查询和重试
现有 media local_path 全部可解析
```

迁移函数应使用新的显式版本判断，不要继续只依赖“是否存在旧 `accounts.id`”这种一次性
启发式。建议在 `storage.py` 中增加 `SCHEMA_VERSION = 3` 和明确的 v2 -> v3 迁移入口。

## 12. 测试要求

### 12.1 Repository/schema

- `x_users` 可以保存未归档外部作者；不要求存在 `observed_accounts`。
- 普通帖子可以被多个 observation 关联，但 `posts` 只有一行。
- 同一个 tweet ID 重复 upsert 不产生重复内容。
- repost 以自己的 tweet ID 幂等。
- 同一 origin 被多个 repost 引用时只有一个 origin `posts`。
- 自转自帖不被合并。
- quote 的 `reference_tweet_id` 正确，外层正文保留。
- reference-only 占位可以在后续详情抓取时补齐。

### 12.2 媒体

- repost 媒体归属 origin，不归属 repost。
- quote 外层媒体和引用媒体归属正确。
- 同一 source URL 在同一 owner 下只创建一个媒体记录。
- 多个 repost 不创建重复媒体下载任务。
- 外部 origin 没有 archive account 时，媒体任务仍可下载、完成和重试。

### 12.3 同步

- 归档账号的普通帖写入 `posts + archive_post_observations`。
- repost 写入 `reposts + archive_repost_observations`。
- 外部 origin 写入 `posts`，但不创建外部账号同步任务。
- 后来添加外部作者为归档账号时复用现有 origin。
- 增量同步重复遇到已知 repost 时不会重复媒体和 observation。
- 来源返回其他顶层作者时仍然拒绝写入当前账号的 observation。

### 12.4 API/frontend

- 账号时间线同时返回 post 和 repost，排序按各自事件时间。
- `post_type=repost` 仍可兼容筛选，但底层来自 `reposts`。
- quote 返回结构化 reference 内容。
- repost 返回独立事件字段和 origin 内容。
- 外部作者可正常展示，没有 `observed_accounts` 外键错误。
- 搜索和 offset 分页不因 origin 内容重复而漏项。

### 12.5 任务中心

- 外部 origin 的媒体任务可以生成任务上下文，不因 JOIN `observed_accounts` 为空而丢失。
- account sync 任务仍可按 archive account 查询、重试和统计。
- media task 仍能通过 media ID 找到 canonical owner 和文件目录。

## 13. 推荐实现顺序

按以下顺序实现，避免一次修改所有层导致无法定位问题：

1. 先实现 schema v3、`x_users`、reposts 和 observation 表，以及 v2 数据迁移。
2. 为 repository 添加独立的 `upsert_post`、`upsert_repost`、`observe_*` 方法和测试。
3. 重构 source 解析结果，让外层媒体、origin 媒体和 quote reference 分开。
4. 重构 sync 入库流程，先处理 canonical content，再处理事件和 observation。
5. 修正媒体 owner、媒体路径和 media task 查询。
6. 修改 API repository 查询，使用 UNION 生成统一 timeline item。
7. 更新 API response model 和前端 timeline/quote/repost 展示。
8. 更新 task center 的媒体上下文查询和失败重试逻辑。
9. 执行迁移校验、后端测试、前端类型检查和生产构建。

## 14. 明确不做的事情

本次模型重构不包含：

- 自动归档所有 `x_users` 的时间线；
- 因为发现外部作者就自动把它加入 `observed_accounts`；
- 用正文、作者和媒体相似度代替 tweet ID 去重；
- 删除已有原始 JSON 或媒体文件；
- 用前端逻辑解析 raw payload 来代替 API 的结构化关系；
- 把 repost 转换成普通 original；
- 把 quote 的新增正文覆盖到被引用原帖上；
- 恢复早期 PostgreSQL/MinIO 架构作为本次实现前提。

## 15. 实现完成标准

当以下条件全部满足时，帖子关系重构才算完成：

```text
外部原帖可以在没有 archive account 的情况下保存
repost 是独立事件但不复制原帖内容和媒体
quote 与普通帖共用 posts 并保留 reference
自转自帖仍然保留两个独立 tweet ID
同一 origin 的媒体只有一个 canonical owner
账号时间线查询不再直接依赖旧模型的 `posts.account_x_user_id`
任务中心不要求每个 media owner 都属于 observed_accounts
旧数据库和旧媒体路径完成可验证迁移
API 和前端不再依赖 raw_payload 推断 repost/quote 展示关系
```
