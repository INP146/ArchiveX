# X 账号身份与 Username 变更处理设计

## 1. 文档目的

本文档记录 ArchiveX 对 X 账号身份的处理原则，解决 username 可修改、旧 username 可被其他用户重新使用所带来的归档目标漂移、数据混合和展示歧义问题。

本文档中的 `username` 指 `@example` 中的 `example`；`x_user_id` 指 X 为账号分配的稳定数字 ID，并以字符串形式保存和传输。

## 2. 问题描述

X 的 username 不是稳定身份：

- 同一个账号可以先后使用多个 username。
- 同一个 username 可以在不同时期属于多个账号。
- 账号改名后，旧 username 可能暂时不存在，也可能被其他账号取得。

因此，username 和 `x_user_id` 是带时间维度的多对多关系，不能将 username 作为账号主键、外键、归档任务标识或文件目录标识。

当前实现虽然在 `accounts` 表中保存了唯一的 `x_user_id`，但同步循环每次仍会重新通过配置中的 username 解析账号。如果原账号改名，系统可能出现以下问题：

1. 旧 username 无人使用时，原账号同步失败并停止更新。
2. 旧 username 被其他账号取得时，系统静默转为归档新所有者。
3. 原账号和新所有者可能在 Web 页面中显示为相同 username。
4. 使用 username 作为归档目录时，不同账号的数据可能进入同一个顶层目录。

## 3. 核心决策

### 3.1 `x_user_id` 是账号身份

- `x_user_id` 作为 `accounts` 表主键。
- 所有数据库外键、同步任务、API 路由和文件目录使用 `x_user_id`。
- `x_user_id` 在 Python、SQLite、JSON 和 TypeScript 中始终作为字符串处理，避免 JavaScript 大整数精度问题。
- 系统不再引入本地 `accounts.id` 代理主键。

### 3.2 Username 仅用于一次性解析和展示

- Web 添加账号时允许输入 username 或账号 URL。
- 后端实时解析出当前 `x_user_id`，并将解析结果展示给用户确认。
- 用户确认后，只将 `x_user_id` 作为归档目标持久化。
- 不持久化“配置 username 永久绑定到某个 `x_user_id`”的映射。
- 后续同步不得通过 username 决定目标账号。
- `current_username` 只表示最近一次观察到的 username，允许为空或过期。

### 3.3 Username 历史是观察记录

- 系统保存账号曾经使用过的 username 及观察时间。
- username 历史表不对 username 设置全局唯一约束。
- username 历史不参与归档目标选择。
- 同一个 username 可以出现在多个 `x_user_id` 的历史记录中。

## 4. 数据模型

### 4.1 `accounts`

```text
x_user_id          TEXT PRIMARY KEY
current_username   TEXT NULL
display_name       TEXT NULL
archive_enabled    INTEGER NOT NULL DEFAULT 1
status             TEXT NOT NULL DEFAULT 'active'
last_sync_at       TEXT NULL
last_error         TEXT NULL
created_at         TEXT NOT NULL
updated_at         TEXT NOT NULL
```

`archive_enabled` 控制是否继续同步。移除归档目标时，默认只设置为禁用，不删除已经归档的数据。

### 4.2 `account_username_history`

```text
id                 INTEGER PRIMARY KEY
x_user_id          TEXT NOT NULL REFERENCES accounts(x_user_id)
username           TEXT NOT NULL
observed_from      TEXT NOT NULL
observed_to        TEXT NULL
last_observed_at   TEXT NOT NULL
```

建议索引：

```text
account_username_history(x_user_id, observed_from DESC)
account_username_history(username COLLATE NOCASE, observed_from DESC)
```

索引用于查询，不代表 username 唯一。username 比较和搜索时进行大小写归一化，但保留最近观察到的原始大小写用于展示。

### 4.3 其他表的外键

```text
posts.account_x_user_id       -> accounts.x_user_id
sync_runs.account_x_user_id   -> accounts.x_user_id
```

帖子继续使用 `tweet_id` 作为主键，媒体继续使用 `tweet_id` 关联帖子。

## 5. Web 添加账号流程

### 5.1 解析

```text
POST /api/accounts/resolve
```

请求中提交 username 或账号 URL。该接口只查询 X 并返回候选账号，不写入归档目标。

返回信息至少包括：

- `x_user_id`
- 当前 username
- 显示名
- 头像
- 简介或其他可帮助确认身份的信息
- 该 `x_user_id` 是否已经存在于 ArchiveX

### 5.2 确认添加

```text
POST /api/accounts
```

用户确认候选账号后提交 `x_user_id`。后端以 `x_user_id` 创建或重新启用归档目标。

username 解析完成后即失去身份作用。即使确认前 username 发生变化，最终添加的仍是确认页面展示的 `x_user_id`。

### 5.3 无法自动消除的歧义

如果 username 在首次添加前已经更换所有者，仅凭 username 无法判断用户原本想归档哪个账号。系统必须展示解析结果供用户确认，并支持直接输入或添加已知的 `x_user_id`。

## 6. 后续账号操作

所有账号操作使用 `x_user_id`：

```text
GET    /api/accounts/{x_user_id}
PATCH  /api/accounts/{x_user_id}
POST   /api/accounts/{x_user_id}/sync
POST   /api/accounts/{x_user_id}/pause
POST   /api/accounts/{x_user_id}/resume
GET    /api/accounts/{x_user_id}/username-history
```

Web 前端路由同样使用字符串形式的 `x_user_id`，不得将其转换为 JavaScript `number`。

## 7. 同步流程

同步循环从数据库读取 `archive_enabled = 1` 的 `x_user_id`，不得从环境变量中的 username 列表持续生成同步目标。

每次同步执行以下步骤：

1. 从数据库取得目标 `x_user_id`。
2. 使用 `x_user_id` 请求账号时间线。
3. 验证返回帖子的作者 ID 与目标 `x_user_id` 一致。
4. 保存帖子和媒体。
5. 从可信响应中更新 `current_username`、显示名和 username 历史。
6. 同步失败时保留原目标 ID，不通过 username 寻找替代账号。

如果账号改名但没有新帖子，`current_username` 可能暂时保持旧值。这是允许的展示延迟，不能因此重新通过旧 username 绑定账号。

## 8. Username 变更处理

观察到同一 `x_user_id` 使用新 username 时：

1. 更新 `accounts.current_username`。
2. 结束上一条当前 username 历史记录。
3. 创建或重新打开新 username 的观察区间。
4. 保留旧帖原始 JSON 中抓取时的 username，不批量重写历史快照。

旧 username 后续属于另一个 `x_user_id` 时，它会作为另一个账号的当前 username 或历史记录存在。两个账号之间不建立继承、合并或自动转移关系。

## 9. 文件目录

新归档目录使用稳定 ID：

```text
accounts/<x_user_id>/posts/<yyyy>/<mm>/<tweet_id>/
  post.json
  media-01.jpg
  media-02.mp4
```

username 不得出现在决定文件归属的目录层级中。展示友好的名称由数据库和 Web 页面提供。

## 10. 环境变量迁移

`ARCHIVE_ACCOUNTS` 不再作为长期同步目标来源。

兼容迁移可以采用以下方式：

1. 首次升级时读取现有 username 列表。
2. 逐个实时解析并要求用户在 Web 页面确认。
3. 确认后写入以 `x_user_id` 为主键的 `accounts` 记录。
4. 完成迁移后停用基于 username 的周期解析。

迁移不能在无确认的情况下自动将旧 username 当前所有者认定为原归档目标。已有数据库中已经保存的 `x_user_id` 应优先作为迁移依据。

## 11. 现有数据迁移原则

- 将现有 `accounts.x_user_id` 提升为主键或新的账号引用键。
- 将 `posts.account_id` 和 `sync_runs.account_id` 迁移为对应的 `x_user_id` 字符串外键。
- 根据现有 `accounts.username` 建立第一条 username 历史观察记录，但不推断准确的历史开始时间。
- 新写入文件使用 `accounts/<x_user_id>/...`。
- 已有文件依据数据库记录迁移，不能依据目录中的 username 猜测所有者。
- 如果发现同一个 username 目录中存在多个 `x_user_id` 的文件，按数据库中的帖子归属拆分，并记录迁移审计日志。
- 迁移前备份 SQLite 数据库和归档目录；迁移过程必须可重复执行或支持回滚。

## 12. 必须保持的系统约束

1. 一个归档账号由且仅由一个 `x_user_id` 标识。
2. username 永远不能改变帖子的账号归属。
3. 同步任务不能因为 username 查询结果变化而切换 `x_user_id`。
4. 返回帖子作者 ID 不匹配目标 ID 时，不得持久化该帖子。
5. username 允许在不同账号的历史中重复出现。
6. 文件路径不得以 username 作为账号归属依据。
7. 禁用账号不得自动删除已有帖子或媒体。

## 13. 验收场景

- 账号从 `alice` 政名为 `alice_new` 后，系统继续同步相同 `x_user_id`。
- `alice` 暂时无人使用时，不影响原账号同步。
- 另一个账号取得 `alice` 后，系统不会自动归档新所有者。
- 用户可以在 Web 中确认并单独添加新的 `alice` 所对应的 `x_user_id`。
- 两个账号的 username 历史都可以包含 `alice`，不会触发唯一约束冲突。
- 服务重启后，同步目标仍然来自数据库中的 `x_user_id`。
- 前端处理超过 JavaScript 安全整数范围的 `x_user_id` 时不会丢失精度。
- 两个曾使用相同 username 的账号，其帖子和媒体位于不同的 ID 目录。
- 同步源返回作者 ID 不一致的帖子时，本轮同步安全失败且不写入错误数据。

## 14. 实施顺序

1. 修改数据库身份模型和迁移逻辑。
2. 将同步循环改为从数据库读取 `x_user_id`。
3. 将归档路径改为基于 `x_user_id`。
4. 增加账号解析、确认添加和启停 API。
5. 完成最小 Web 账号管理界面。
6. 增加 username 历史展示和身份异常审计。
7. 完成旧数据和环境变量配置迁移。

