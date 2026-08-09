# ArchiveX 队列系统审查报告

- 审查日期：2026-08-08
- 二次复核：2026-08-09
- 审查范围：Taskiq/Redis 队列、调度器、重试、去重锁、任务中心、部署与健康检查
- 审查方式：静态代码审查、已安装 Taskiq 0.12.x 行为核对、现有 Redis/SQLite 运行数据只读抽样、自动化测试
- 测试结果：`59 passed, 1 warning`（`.venv/bin/pytest -q`）

> 本文保留首次审查时的故障证据。2026-08-09 二次复核发现第一次修复仍缺少统一生命周期模型，补充为 Q-08；最终修复与实现边界见 [`QUEUE_SYSTEM_FIXES.md`](./QUEUE_SYSTEM_FIXES.md)。

## 1. 结论

首次审查确认的 Q-01 至 Q-07 均为真实问题，不是伪问题。但二次复核也确认：第一次修复虽然解除了 Dashboard HTTP 上报对执行面的阻塞，却错误地认为“异步请求 + 单行 upsert”足以处理乱序和重试。这个判断不成立，因此新增一个结构性高风险问题 Q-08。

最优先需要解决的是：

1. 永久媒体失败不会释放去重锁，导致任务中心显示“可重试”但实际无法立即重试。
2. Dashboard 上报位于发布和执行的关键路径；Dashboard/API 短暂不可用会阻止任务发布，或让已消费消息等待完整的 Redis reclaim 周期，调度触发还可能整轮丢失。
3. 发布失败的回滚依赖同一个故障中的 Redis，回滚再次失败时会留下永久 `queued` 记录和最长一小时的阻塞锁。
4. 第三方 Dashboard 的“一任务一行”模型无法表达自动重试的多次 attempt，并发事件乱序还会让迟到的 `queued` 覆盖失败或完成状态。
5. taskiq-redis 只在读到新 Stream 消息后回收 pending；队列安静时，旧 consumer 遗留的 pending 永远不会被接管。
6. 本地启动脚本不检查已脱离父进程的旧 worker/scheduler，重复启动会产生两套消费者和调度器；broker 默认批量领取 100 条，又会让单个旧 consumer 占住远超处理能力的消息。

## 2. 审查时的原架构

```text
Taskiq Scheduler
      |
      v
archivex:crawl (Redis Stream) ---> crawl worker (并发 1)
      |                                  |
      |                                  +-- 账号同步
      |                                  +-- 分发媒体任务
      v
archivex:media (Redis Stream) ---> media worker (并发 4)
                                         |
                                         +-- gallery-dl

所有生产者/消费者 --同步 HTTP 上报--> FastAPI 挂载的 Taskiq Dashboard
去重状态             --Redis TTL 锁--> archivex:dedupe:*
任务展示             --SQLite-------> taskiq-dashboard.sqlite3
```

队列消息采用 Redis Stream，默认在结果保存后确认；未确认消息由 `XAUTOCLAIM` 回收。账号和媒体分别使用独立 Stream，任务副作用通过 SQLite upsert、媒体状态和 Redis 所有权锁实现幂等。

## 3. 问题汇总

| ID | 严重度 | 问题 | 主要影响 |
| --- | --- | --- | --- |
| Q-01 | 高 | 永久媒体失败不释放去重锁 | 一键重试返回旧失败任务，默认阻塞 1 小时 |
| Q-02 | 高 | Dashboard 上报位于队列关键路径 | API 短暂故障可阻止发布、延迟消费或漏掉整轮调度 |
| Q-03 | 中 | 发布失败回滚不是故障安全的 | 留下无法完成的 `queued` 记录和残留锁 |
| Q-04 | 中 | 调度器重启会立即执行 interval 任务 | 频繁重启造成全账号重复同步和上游限流压力 |
| Q-05 | 中 | 去重 TTL 与任务超时没有配置约束或续租 | 非默认配置下同一媒体可并发下载 |
| Q-06 | 中 | 健康检查无法反映队列是否工作 | Redis、worker、scheduler 停止时系统仍报告健康 |
| Q-07 | 低 | API 启动会修改 worker 所有的同步运行状态 | API 单独重启时任务历史短暂误报为 interrupted |
| Q-08 | 高 | 没有统一的逻辑任务/执行尝试模型 | 状态乱序、attempt 错位、自动与手动重试关系不清，任务表现为长期等待 |
| Q-09 | 高 | pending reclaim 依赖先读到新消息 | 队列没有新任务时，崩溃 worker 遗留的任务永久卡住 |
| Q-10 | 高 | 启动脚本允许重复队列进程 | 两套 scheduler/worker 同时运行，旧代码继续消费新任务并上报已删除接口 |
| Q-11 | 中 | Stream 领取批量大于 worker 处理槽 | 单个 consumer 可占住大量消息，其他健康 worker 无法及时接管 |

## 4. 详细发现

### Q-01：永久媒体失败不释放去重锁（高）

**证据**

- `ResilientSmartRetryMiddleware.on_error()` 遇到 `PermanentMediaDownloadError` 时直接返回，没有释放锁：`src/archivex/tasks.py:89-96`。
- `download_media_task()` 仅在 `_is_final_attempt()` 为真时释放失败任务的锁：`src/archivex/tasks.py:401-409`。
- 永久错误发生在首次尝试时，`_retries=0`、`max_retries=5`，因此不属于最后一次尝试：`src/archivex/tasks.py:264-267`。
- 任务中心又将永久错误标记为自动重试已耗尽、允许手工重试：`src/archivex/task_center.py:264-275`。
- 手工重试最终调用同一个去重入口；锁仍存在时只返回旧任务 ID，并标记为 duplicate：`src/archivex/tasks.py:288-303`。

只读运行抽样也验证了该路径：一个 `PermanentMediaDownloadError` 任务结束后，对应 `archivex:dedupe:media:*` 锁仍由该失败任务持有，抽样时 TTL 还有 3289 秒。也就是说，UI 的“重试”操作会在默认约 1 小时内被静默合并到已经失败的任务。

**建议**

- 在永久错误分支中调用 `_release_failed_retry_lock(message)` 后再返回。
- 更稳妥的设计是让任务执行层明确返回“是否还会重试”，所有终态统一在一个 `finally`/终态钩子中释放锁，避免业务异常分类和锁生命周期分散在两个中间件中。
- 增加集成测试：永久错误后 Redis 锁必须不存在，下一次 `enqueue_media_download()` 必须生成新任务 ID。

### Q-02：Dashboard 上报位于队列关键路径（高）

**证据**

- `_spawn_request()` 被改为同步等待 HTTP 响应，并对非 2xx 抛错：`src/archivex/tasks.py:54-61`。
- `pre_send()` 在 Redis Stream 发布之前调用 Dashboard 的 queued 接口：`src/archivex/tasks.py:63-67`。
- `pre_execute()` 在任务执行、消息确认之前调用 Dashboard 的 started 接口：`src/archivex/tasks.py:72-76`。
- Dashboard 运行在独立 API 服务中，worker 和 scheduler 通过 HTTP 访问：`docker-compose.yml:57-67,83-133`。

结合 Taskiq 0.12.x 的实际调用顺序：

- `pre_send` 失败时，消息尚未写入 Redis。手工请求会报错；scheduler 的 interval 触发已经在内存中记为本轮已运行，但任务没有持久化，因此默认要等下一次 6 小时间隔。
- `pre_execute` 失败时，消息已经进入 consumer group 但尚未确认。它不会执行，只能等 `idle_timeout` 后被回收；当前抓取队列约为 31 分钟，媒体队列约为 6 分钟。
- 因此监控平面的短暂故障会直接影响数据平面的可用性，与“Dashboard 仅负责观测”的边界不符。

**建议**

- Dashboard 上报应为 best-effort，不得阻止 broker publish 或任务执行；失败时记录日志/指标并继续。
- queued 事件应放在发布成功之后。为解决“任务执行快于 queued 事件”的顺序问题，让 Dashboard 的 started/executed 接口具备 upsert 能力，或使用独立事件 Stream/Outbox 按事件序列归并。
- scheduler 的触发需要持久化，只有任务成功写入 broker 后才能提交本轮执行时间。
- 增加 API/Dashboard 宕机故障注入测试，分别覆盖 producer、worker 和 scheduler。

### Q-03：发布失败回滚不是故障安全的（中）

**证据**

- 发布异常后 `_rollback_failed_publish()` 首先调用 Redis 删除锁，随后才把 Dashboard 行标为 abandoned：`src/archivex/tasks.py:254-261`。
- 如果 Redis `XADD` 因连接中断失败，紧接着的 Redis `EVAL` 很可能也失败。此时函数提前抛出，Dashboard 更新不会执行。
- queued 事件在真正发布之前已经写入 Dashboard，见 Q-02。

结果是：消息不在 Stream 中，但任务中心会永久显示 `queued`；去重锁可能保留到 `TASK_DEDUPE_TTL_SECONDS` 到期（默认 3600 秒），期间新提交被误判为重复任务。现有测试只模拟“broker 失败、锁删除成功”，没有覆盖两步同时失败：`tests/test_tasks.py:173-194`。

**建议**

- 锁释放和 Dashboard 终态更新分别放入独立的 `try/except`，确保任一清理失败不阻止另一项清理，并保留原始 publish 异常。
- abandoned 更新应具备重试或后台校正机制；任务中心可定期把“超过阈值且 broker/result/lock 均不存在”的 queued 行标为 abandoned。
- 增加 `XADD` 成功/失败不确定、锁释放失败、Dashboard 更新失败的组合测试。

### Q-04：调度器重启会立即执行 interval 任务（中）

**证据**

- 定时任务使用 `LabelScheduleSource` 的 interval 标签：`src/archivex/tasks.py:157-160,415-422`。
- Taskiq 0.12.x 的 label source 每次启动生成新的 schedule ID，interval 的 `last_run` 只保存在 scheduler 进程内存中；首次检查会立即判定为到期。
- 只读 Dashboard 数据中，`ARCHIVE_SYNC_INTERVAL_SECONDS=21600`（6 小时），但 3 小时内出现了 9 次 scheduler 任务，schedule ID 均不同，时间与 scheduler 重启相符。

单次启动立即同步未必错误，但当前语义和 README 的“按间隔同步”不一致；频繁部署或崩溃重启会反复抓取全部启用账号，增加 X 限流和媒体分发压力。

**建议**

- 使用持久化 schedule source 保存固定 schedule ID 和下一次运行时间，或将上次成功分发时间写入 Archive DB/Redis。
- 明确定义启动策略：`run_immediately_on_start` 应为显式配置，而不是依赖 Taskiq interval 的隐含行为。
- 增加“scheduler 重启后、距离上次运行未满 interval 不分发”的测试。

### Q-05：去重 TTL 与任务超时没有配置约束或续租（中）

**证据**

- `TASK_DEDUPE_TTL_SECONDS`、同步超时和媒体超时仅有各自下界，没有交叉校验：`src/archivex/config.py:29-35`。
- 锁只在入队和任务开始时设置/刷新，执行过程中没有心跳续租：`src/archivex/tasks.py:191-224`。
- 媒体 worker 并发为 4：`docker-compose.yml:104-120`。

默认 TTL 3600 秒大于同步/媒体超时，当前默认值安全。但配置允许 `TASK_DEDUPE_TTL_SECONDS=60`、`TASK_MEDIA_TIMEOUT_SECONDS=300`。长下载运行 60 秒后锁会消失，新提交可由另一个并发槽获取新锁并同时下载同一媒体，破坏“同目标不并发”的保证。

**建议**

- 启动时校验 `dedupe_ttl > max(sync_timeout, media_timeout) + reclaim_margin`；同时考虑最大调度重试间隔。
- 对长任务实现带所有权校验的周期续租，续租失败时输出高优先级告警。
- 增加缩短 TTL 的并发测试，验证同一媒体始终只有一个执行者。

### Q-06：健康检查无法反映队列是否工作（中）

**证据**

- `/health` 无条件返回 `{"status": "ok"}`：`src/archivex/main.py:136-138`。
- Compose 只为 Redis、API 和 Web 配置 healthcheck；crawl worker、media worker 和 scheduler 没有健康检查：`docker-compose.yml:45-133`。
- 任务中心读取 SQLite 失败时会返回空列表而不是暴露降级状态：`src/archivex/task_center.py:50-65`。

因此 Redis 断开、两个 worker 全停、scheduler 停止或 Dashboard DB 锁死时，外部仍可能看到健康状态。队列积压只能由人工观察发现。

**建议**

- 区分 API liveness 与系统 readiness。
- readiness 至少检查 Redis ping、两个 Stream 的 pending/lag、最近 worker heartbeat、最近 scheduler 分发时间和 Dashboard DB 可读写性。
- 对最老 queued/pending 年龄、reclaim 次数、连续调度失败设置告警。
- 不要让队列 readiness 反过来阻止 Dashboard/API 作为观测端启动，避免加重 Q-02 的耦合。

### Q-07：API 启动会修改 worker 所有的同步运行状态（低）

**证据**

- API lifespan 每次启动都会把所有 `running` sync run 标为 `interrupted`：`src/archivex/main.py:69-82`。
- API 与 crawl worker 是独立进程/容器：`docker-compose.yml:57-67,83-103`。

只重启 API 时，crawl worker 可能仍在正常同步。此时运行记录会被错误标为 interrupted，待 worker 最终写回 success/error 后才恢复正确；若任务仍在运行，UI 和审计数据会短暂失真。

**建议**

- 由持有任务的 worker 更新自身 sync run；恢复逻辑应基于 worker lease/heartbeat 过期，而不是 API 进程启动。
- 状态更新增加所有权 token 或条件更新，避免无关进程覆盖活动运行。

### Q-08：任务生命周期模型无法表达重试并抵抗乱序（高）

**证据**

- Taskiq 自动重试复用原 `task_id`，手动重试创建新 `task_id`；这是两种不同操作语义，但原任务中心既没有 attempt 表，也没有 `retry_of` 关系。
- taskiq-dashboard 0.4.4 只保存“一任务一行”。同一 ID 的第 2 至第 N 次自动重试会覆盖上一轮信息，无法回答“执行过几次、每次为什么失败、下次何时运行”。
- queued、started、executed 通过独立并发 HTTP 请求上报，没有同一任务的顺序保证。即使接口支持缺行 upsert，迟到的 queued 仍可覆盖已到达的 failure/completed；“可 upsert”不等于“可处理乱序”。
- SmartRetry 构造下一次 retry kicker 时直接复用 `message.labels`。`with_labels(_retries=...)` 会原地修改当前消息，导致当前 attempt 在完成事件上报前提前加一。
- 延迟重试保存在独立 Redis schedule 键中，原健康检查只观察 Redis Stream。任务离开 Stream、等待 retry schedule 时，系统无法区分“正常等待下次尝试”和“scheduler 已失效”。

指定任务 `f0b757aa-c02d-46de-afda-fd7f665d7211` 的最终只读记录也验证了真实执行故障：它在第 5/5 次失败，底层错误为旧 media worker 启动 Python 子进程时继承失效标准文件描述符，报 `OSError: [Errno 9] Bad file descriptor`。这不是单纯的 UI 文案问题。

**建议**

- 由应用自己维护 `logical task` 与 `attempt` 两层生命周期，不再让第三方 Dashboard 充当队列状态源。
- 所有事件更新必须以 `(task_id, attempt)` 为条件，终态不可被同 attempt 的迟到 queued/started 降级，高 attempt 才可推进逻辑任务。
- 自动重试沿用逻辑任务 ID 并增加 attempt；手动重试创建新逻辑任务，通过 `retry_of` 关联来源。
- retry schedule 必须写入明确的 `retry_scheduled` 和 `next_retry_at`，readiness 同时检查 Stream、heartbeat 和过期 retry bucket。
- 构造下一次 retry 消息前复制 labels，禁止修改当前 attempt 的消息元数据。

### Q-09：队列安静时不会回收旧 pending（高）

**证据**

- taskiq-redis 1.2.3 的 `RedisStreamBroker.listen()` 在 `XREADGROUP` 没有返回新消息时直接 `continue`，`XAUTOCLAIM` 位于该分支之后。
- 因此 reclaim 不是周期行为，而是由另一条新消息触发。最后一条消息被旧 worker 消费但未确认后，如果队列不再入队，它永远没有机会被接管。
- 现场只读检查发现 media 与 crawl Stream 各有 1 条 pending，idle 均约 16 小时、delivery count 均为 1、lag 为 0；对应生命周期行仍为 queued，retry schedule 和去重锁均不存在。
- 两条现场任务分别为 media attempt 4/5 的 `ef1b5e51-281a-4ab6-a818-9b395d2fd973`，以及 crawl attempt 1/5 的 `f5c7176b-ee58-4760-a459-ecf11d8815da`。这直接证明“队列前面没任务却一直等待”不是正常退避。

**建议**

- 覆盖 broker 的 listen 循环，每轮先执行 `XAUTOCLAIM`，再阻塞读取新消息；reclaim 不得依赖新流量。
- 多 worker 使用短期所有权锁串行化 reclaim，锁不可用时跳过本轮，不阻塞正常消费。
- 增加“`XREADGROUP` 始终为空但存在过期 pending”测试，必须仍能产出并执行该消息。

### Q-10/Q-11：重复进程与过量预取破坏消费者所有权

**证据**

- `scripts/start_backend.py` 原先只检查新启动的子进程，不扫描同一项目已经存在的 Taskiq 进程。父启动器被强制退出后，使用独立 session 的 worker/scheduler 会成为孤立进程，下一次启动仍会再创建一套。
- 2026-08-09 17:54 现场同时出现昨晚启动的 3 个旧 Taskiq 进程和刚启动的 3 个新进程。旧 worker 继续向已删除的 `/ops/tasks/.../started` 上报并返回 404，证明新旧代码同时消费同一 Stream。
- taskiq-redis 默认 `xread_count=100`、`unacknowledged_batch_size=100`，但 crawl/media 实际处理槽分别只有 1/4。现场旧 media consumer 一次持有 618 条 pending，新 worker 的 Stream lag 虽为 0，却无法取得这些已经分配的消息。

**建议**

- 本地启动前扫描项目专属 Taskiq 命令，发现存活 worker/scheduler 时拒绝启动并列出 PID，要求先清理旧 stack。
- `XREADGROUP` 和 `XAUTOCLAIM` 批量必须与对应 worker 并发数一致，crawl 为 1、media 为 4，避免 consumer 囤积超出执行槽的消息。
- readiness 不应只看 lag；pending 数、最老 idle、consumer heartbeat 必须联合判断。

## 5. 已有优点

- 抓取与媒体使用独立 Stream 和并发策略，避免媒体下载阻塞账号抓取。
- Redis 开启 AOF，Taskiq 使用执行后/结果保存后确认，并设置了基于任务超时的 reclaim idle time。
- 去重锁使用带 owner 比对的 Lua 删除，避免旧任务删除新任务的锁：`src/archivex/tasks.py:39-44,227-232`。
- 帖子和媒体写入具备幂等约束；媒体下载使用独立临时目录，完成后再移动到最终路径。
- 重试调度设置了连接超时、指数退避、抖动和最大延迟；重试调度自身失败时尝试转终态。
- 任务中心能够区分当前失败、已被后续任务取代的历史失败和仍在自动重试的失败。

## 6. 测试覆盖评估

首次审查时 59 个测试全部通过，但只覆盖了基本去重、发布失败后的正常回滚、Dashboard 状态重置、永久错误分类、下载超时和任务中心 API。它们没有证明完整任务生命周期正确。主要缺口集中在跨组件故障与真实任务生命周期：

- 永久错误后的锁释放与立即重试。
- Dashboard/API 在 pre-send、pre-execute、post-execute 阶段不可用。
- Redis 在 reserve 成功后、publish/rollback 阶段断开。
- scheduler 重启及 interval 持久化。
- worker 被终止后的 Redis Stream reclaim。
- 去重 TTL 过期时的并发执行。
- API 重启时仍有 crawl worker 活动。
- 自动重试多 attempt 历史、事件乱序和 queued/started 丢失后的终态恢复。
- retry schedule 逾期但 Stream 中没有消息时的 readiness。
- Stream 没有新消息时对旧 consumer pending 的主动 reclaim。

建议增加 Redis + API + worker 的最小集成测试环境；当前多数队列测试使用 FakeRedis/FakeTask，只能验证单函数分支，无法证明消息发布、确认、重试调度和锁状态的原子关系。

## 7. 建议修复顺序

1. 修复 Q-01，保证所有终态都释放 owner 锁，并补回归测试。
2. 修复 Q-02，把 Dashboard 从发布/执行关键路径移除，再做 API 故障注入。
3. 修复 Q-03，使发布失败清理可独立完成并可校正僵尸 queued 行。
4. 持久化调度状态，明确启动即运行策略（Q-04）。
5. 加配置不变量和锁续租（Q-05）。
6. 增加队列 readiness、heartbeat、积压指标和告警（Q-06）。
7. 将 sync run 恢复逻辑迁移到 worker lease 模型（Q-07）。
8. 用应用自有的逻辑任务/attempt 存储替换第三方 Dashboard 生命周期模型（Q-08）。
9. 修复 broker reclaim 循环，确保安静队列也能接管过期 pending（Q-09）。
10. 阻止同一项目重复启动 Taskiq 进程（Q-10）。
11. 将 Stream 新消息读取和 pending reclaim 批量限制为实际 worker 并发（Q-11）。

完成前 3 项后，队列的失败语义才基本符合“任务至少一次、可立即人工恢复、观测面故障不影响执行面”的可靠性目标。
