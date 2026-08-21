# ArchiveX 队列系统审查修复记录

> 2026-08-21 Docker Desktop SQLite `SIGBUS`、consumer 心跳与 orphan pending
> 的现场修复另见 `DOCKER_SQLITE_RECOVERY.md`。

- 修复日期：2026-08-08 至 2026-08-09
- 依据：`QUEUE_SYSTEM_REVIEW.md`
- 基线测试：`59 passed, 1 warning`
- 修复后测试：`77 passed, 1 warning`

## 1. 复核结论

修复前重新核对了当前源码、Taskiq 0.12.4 与 taskiq-dashboard 0.4.4 的已安装实现、Redis Stream/SQLite 只读运行数据。报告中的 Q-01 至 Q-07 均为真实问题，没有伪问题。2026-08-09 对第一次修复再次复核后，确认“异步 HTTP 上报 + Dashboard upsert 可以处理乱序”的结论错误，并补充结构性问题 Q-08。

| ID | 复核结果 | 关键依据 |
| --- | --- | --- |
| Q-01 | 准确 | 永久媒体异常绕过 SmartRetry，业务任务又只在最后一次尝试释放锁 |
| Q-02 | 准确 | 自定义 Dashboard 请求同步等待；`pre_send` 位于 Redis `XADD` 前，`pre_execute` 位于任务调用前 |
| Q-03 | 准确 | publish 失败后的 Redis 解锁异常会跳过 Dashboard 清理 |
| Q-04 | 准确 | Taskiq interval 在 `last_run is None` 时立即到期，`last_run` 仅在 scheduler 进程内存中 |
| Q-05 | 准确 | 原配置允许 dedupe TTL 小于任务硬超时，执行中锁可先过期 |
| Q-06 | 准确 | 原 `/health` 无条件成功，且没有 worker/scheduler 存活信号 |
| Q-07 | 准确 | API lifespan 会无条件结束独立 crawl worker 所有的 running sync run |
| Q-08 | 准确 | 第三方 Dashboard 只有单行任务状态，无法表达多 attempt；HTTP 事件无序且 retry labels 会被原地修改 |
| Q-09 | 准确 | taskiq-redis 只有在 `XREADGROUP` 返回新消息后才运行 `XAUTOCLAIM`，安静队列不会回收 pending |
| Q-10 | 准确 | `start_backend.py` 不检查同项目已有 Taskiq 进程，孤立旧进程与新 stack 会并存 |
| Q-11 | 准确 | broker 默认批量 100，远大于 crawl/media 的 1/4 个实际处理槽 |

## 2. 最终架构

```text
producer / scheduler ---> Redis Stream ---> crawl/media worker
         |                                      |
         +-- publish failure -------------------+-- TaskLifecycleMiddleware
                                                    |
                                                    v
                         archive SQLite: archive tables + queue_tasks + queue_attempts

automatic retry: same logical task ID, attempt + 1, persisted retry_scheduled
manual retry:    new logical task ID, linked to the source task with retry_of
readiness:       archive SQLite + Redis + worker/scheduler heartbeat + Stream pending/lag
                 + overdue retry schedule buckets
```

任务执行不依赖 FastAPI 或任务中心页面在线。任务中心直接读取统一的归档数据库；Redis Stream、retry schedule、去重锁和 heartbeat 由 readiness 联合观测。

## 3. 已实施修复

### Q-01 终态锁释放

- `PermanentMediaDownloadError` 分支在停止自动重试前释放 owner 匹配的媒体锁。
- 保留 Lua owner 比较，旧任务不能删除新任务的锁。
- 回归测试验证永久错误不会触发 retry，并且会释放锁。

### Q-02 与 Q-08 统一生命周期模型

- 删除 `MountedTaskiqAdminMiddleware`、挂载的 `/ops/tasks` 和 `taskiq-dashboard` 依赖，不再通过 HTTP 上报任务事件。
- 新增本地 `TaskLifecycleMiddleware`，worker 直接写生命周期 SQLite；入队和开始事件写入失败只记录异常，不阻止 broker publish 或业务执行；终态 `record_finished` 会短暂重试，最终失败则让 Taskiq 保留消息 pending，等待 reclaim 后重投，避免出现“Redis 成功但 SQLite 仍 in_progress”。
- 自动重试先持久化 `retry_scheduled`，再创建 Redis time schedule；前者失败时不创建 schedule、不 ACK 原 Stream 消息，避免“Redis retry 已存在但 SQLite 仍 in_progress”。旧 attempt 或已有终态拒绝再次创建 retry。
- retry schedule 使用 `<task_id>:retry:<retry_number>` 确定性 ID，并通过 Lua 以 `LREM -> RPUSH -> SET` 原子提交时间桶和 data key；data key 是最终提交标记。重复添加不会重复入桶，客户端响应丢失时会同时核验 data key 与时间桶，无法核验则保留原 Stream 消息 pending。
- `queue_tasks` 保存逻辑任务，`queue_attempts` 保存每次执行尝试。自动重试保持同一逻辑任务 ID 并递增 attempt，手动重试创建新任务并通过 `retry_of` 关联来源。
- queued/started/finished/retry 更新带 attempt 条件：同 attempt 的迟到事件不能把 completed/failure 降级，只有更高 attempt 能推进逻辑任务。
- `record_finished` 和 `record_retry_scheduled` 可在 queued/started 全部丢失时补建任务与正确 attempt，不再依赖理想事件顺序。
- 新增 `retry_scheduled` 和 `next_retry_at`，UI 显示当前 attempt、最大次数、每次错误和下次重试时间；等待自动重试期间禁止重复手动重试。
- SmartRetry 使用 `dict(message.labels)` 构造下一次消息，避免 `_retries` 污染当前 attempt；只有确实安排成功的 retry 才隐藏 result backend 的当前失败结果。

2026-08-09 后续将任务生命周期表和历史数据一次性合并进 `ARCHIVE_DB_PATH`，删除独立任务库配置以及运行时迁移兼容层。迁移前快照压缩归档到 `backups/pre-database-consolidation-20260809T112618Z.tar.gz`，活动数据目录不再保留旧任务库。

### Q-03 publish 失败清理

- Redis 解锁与 lifecycle abandoned 更新使用独立异常边界。
- 清理失败不会覆盖原始 publish 异常。
- publish 失败会直接补建 abandoned 生命周期记录，不留下伪 queued 行。
- 回归测试覆盖 publish 与 Redis rollback 同时失败时生命周期清理仍会执行。

Redis `XADD` 已提交但响应丢失时仍可能出现重复消息，这是分布式发布无法从单次连接错误中判定的固有歧义；系统继续按至少一次语义和幂等副作用处理。

### Q-04 scheduler 重启门控

- interval schedule 使用固定 schedule ID。
- Redis 保存最近一次成功分发时间，并用 owner lease 串行化并发 scheduler 调用。
- scheduler 重启后仍会收到 Taskiq 的首次 interval 任务，但未满间隔时只做门控检查，不再分发账号同步。
- 新配置 `ARCHIVE_SCHEDULE_RUN_IMMEDIATELY_ON_START` 显式控制首次启动是否立即分发，默认 `false`。
- 只有完整分发成功才提交最近分发时间；异常会释放 lease 并交给 Taskiq retry。

### Q-05 dedupe TTL 不变量

- Settings 启动校验要求 dedupe TTL 至少覆盖同步超时、媒体超时、最大 retry delay 中的最大值，再加 60 秒 reclaim margin。
- claim/refresh 改为 Lua owner 校验的原子操作，旧任务不能在锁换主后续期新 owner 的锁。
- 当前任务执行受同步 `asyncio.timeout` 和 gallery-dl subprocess timeout 硬限制；每次 retry 执行会刷新锁 TTL，账号同步批量分发媒体时也会逐项续租。
- 非法配置在 API、worker、scheduler 启动时直接失败，不再允许锁早于执行超时过期。

### Q-06 liveness、readiness 与 heartbeat

- `/health` 保持 API liveness，不访问 Redis 或 Dashboard。
- 新增 `/ready`，检查统一 Archive SQLite、Redis、crawl/media worker 与 scheduler heartbeat。
- readiness 返回两个 Stream 的 consumer、lag、pending、最老 pending idle 时间和投递次数；超过任务 timeout 加 120 秒仍 pending 时报告 stalled。
- readiness 同时统计独立 retry schedule，并在时间桶逾期仍有任务时报告 stalled，避免“Stream 为空但任务永远等待”的盲区。
- `taskiq-redis 1.2.3` 无法解析带冒号 prefix 的过期时间桶；本地 schedule source 按完整 `archivex:retry-schedules:time:` 前缀解析，使错过当前分钟的 retry 能在下一轮恢复。
- worker 与 scheduler 每 10 秒写入 TTL 30 秒的 owner heartbeat，优雅退出只删除自己持有的 key。
- Compose 为三个进程增加 heartbeat healthcheck，但 API/worker 启动不以 `/ready` 互相依赖。

readiness 提供机器可读状态；告警规则和外部通知渠道仍需由部署环境接入。

### Q-07 sync run 所有权

- API lifespan 不再修改 running sync run。
- `ArchiveSyncService` 在实际开始某账号同步前，只结束该账号遗留的 running run，然后创建本次 run。
- 其他账号仍在运行的记录不会被修改；queue disabled 的 inline 执行也使用同一恢复语义。

### Q-09 pending 主动回收

- 新增 `ReclaimingRedisStreamBroker`，每轮消费先尝试 `XAUTOCLAIM` 过期 pending，再阻塞等待新 Stream 消息。
- reclaim 使用 Redis 所有权锁串行化；其他 worker 已持锁时直接跳过本轮，不阻塞正常消费。
- consumer 心跳只用于 readiness 告警，不会在 40 秒后直接抢占仍可能运行的任务；
  `XAUTOCLAIM` 仍遵守对应任务硬超时加保护期，避免重复抓取。
- 回归测试验证：即使 `XREADGROUP` 没有任何新消息，broker 仍会先产出旧 consumer 的过期 pending。
- 现场遗留的 2 条 pending 无需修改 Redis 数据；加载新 broker 的 worker 启动后会按原至少一次语义自动接管。

### Q-10/Q-11 进程与领取边界

- `scripts/start_backend.py` 启动前扫描当前项目 `.venv/bin/taskiq` 的 worker/scheduler；发现任何存活实例就拒绝重复启动并列出 PID。
- broker 的 `xread_count` 与 `unacknowledged_batch_size` 改为对应队列并发数：crawl 为 1，media 为 4。
- 本地启动脚本和两份 Compose 显式固定 `--ack-type when_saved`，确保终态 SQLite 写入位于 Redis result 和 Stream ACK 之前。
- 新增进程发现测试，确保其他项目的同名 Taskiq 进程不会被误判。

### 媒体子进程标准输入

- `gallery-dl` 子进程显式使用 `stdin=subprocess.DEVNULL`，不再继承已孤立 worker 的失效标准输入描述符。
- 该修复针对任务 `f0b757aa-c02d-46de-afda-fd7f665d7211` 第 5/5 次失败中观察到的 `init_sys_streams` / `Bad file descriptor`。
- 旧 worker 进程不会热加载此修改，必须在确认当前无运行任务后重启才会生效；本次修复过程未操作现有进程。

### 2026-08-21 生命周期分叉与 retry 时间桶事故

- 任务 `ca50197d-8f45-481a-b19b-07f6bd74c915` 实际于北京时间 `2026-08-21 00:11:17` 成功完成，但 SQLite 生命周期终态写入异常被吞，任务中心长期显示 `in_progress`；Redis result、同步运行和媒体结果均证明业务已经完成。
- 修复前备份为 `/Users/user/Projects/docker/ArchiveX/data/backups/pre-task-repair-20260821T094550Z.sqlite3`；现场任务已依据 Redis 结构化成功结果恢复为 `completed`，没有重跑抓取，也没有启动、停止或重启容器。
- 当前开发 Compose 的宿主入口是 `http://127.0.0.1:8400`，容器内部 API 是 `8000`；宿主 `8000` 属于另一项服务，不能用于判断 ArchiveX readiness。
- 当前开发栈的 `8400/ready` 已无 overdue retry；历史部署目录的 `8300` 使用旧 GHCR Compose，不能与开发栈状态混用。

### 2026-08-21 twscrape crawl 子进程锁恢复

- Docker 现场的批次延迟来自 twscrape 唯一抓取账号被锁后，每个任务固定等待 30 秒；健康请求本身仍约 1 秒。
- `TWSCRAPE_WAIT_TIMEOUT_SECONDS` 降为 0.5 秒，`AccountPoolUnavailableError` 携带最早解锁时间，所有受影响任务在同一时间点集中重试。
- 适配器拦截 twscrape 0.19.2 的 GraphQL error 336 `exit(1)` 路径并释放请求上下文，避免 crawl 子进程无日志退出。
- crawl 子进程持有 `accounts.db` 旁的 advisory lease；只有检测到上一个 lease 是非正常退出且当前没有其他持有者时才清理 ownerless locks，正常停止保留真实 rate-limit lock。详见 `TWSCRAPE_WORKER_RECOVERY.md`。

## 4. 验证

执行：

```text
.venv/bin/pytest -q
```

最新结果：`128 passed, 1 warning`。warning 来自现有 FastAPI TestClient 对 `httpx` 的弃用提示，与本次队列改动无关。

另外执行了 Python 全量语法编译、`git diff --check`、Compose 配置解析和前端生产构建。验证过程中没有启动、停止或重启现有 API、worker、scheduler、Redis 或 Vite 进程。
