# ArchiveX v0.1.2-rc1 发布说明

## 发布状态

`v0.1.2-rc1` 是基于 `v0.1.1` 的候选版本，发布时间为 2026-08-22。
该版本重点修复 Docker Desktop 上 SQLite 状态盘、任务队列恢复和 twscrape
worker 稳定性问题，并统一本地构建与镜像部署配置。

这是 release candidate，不是最终稳定版。生产升级前必须完成下方的备份、
迁移和全新部署验证，并确认 GHCR 中的两个多架构镜像已经发布。

版本标识如下：

- Git tag：`v0.1.2-rc1`
- Python 包：`0.1.2rc1`（PEP 440 格式）
- Web、Docker 镜像：`0.1.2-rc1`

## 主要变更

### Docker SQLite 状态与迁移

- 开发构建 Compose 将 `archive.sqlite3` 和 twscrape `accounts.db` 放入 Docker
  Linux named volume，归档媒体仍保留在宿主机 `./data/archive`。
- 新增 `state-migrate` 一次性迁移服务，使用 SQLite backup API、完整性检查和
  原子替换迁移旧数据，不修改或删除旧目录。
- 镜像入口只修正状态文件和媒体根目录的所有权，不再递归处理整个媒体树，避免
  大型归档阻塞容器启动。
- `/ready` 现在会报告数据库、Redis、worker/scheduler 心跳以及异常 pending
  和过期 retry schedule。

### 队列生命周期与恢复

- `queue_tasks` 和 `queue_attempts` 与归档业务表统一存放在
  `ARCHIVE_DB_PATH`，任务中心不再依赖独立 Dashboard 数据库或 API 事件上报。
- 自动重试使用同一逻辑任务递增 attempt，手动重试创建新任务并通过
  `retry_of` 关联来源；迟到事件不能把终态任务降级。
- retry schedule、下次重试时间、每次错误和任务终态都持久化，避免任务在
  Redis Stream 为空时失去可见性。
- worker 每轮消费前主动执行 `XAUTOCLAIM`，安静队列中的过期 pending 也能被
  回收；领取批量和 worker 并发数保持一致，降低过量预取风险。
- 固定 `ack-type=when_saved`，只有生命周期写入完成后才确认 Redis 消息。
- 增加去重 TTL、任务硬超时、锁续租和 owner 校验，避免旧任务释放或续租新任务
  的锁。

### twscrape 与媒体 worker 稳定性

- 账号池暂时不可用时快速失败并按最早解锁时间集中重试，不再让每个任务固定等待
  30 秒。
- 处理 twscrape 0.19.2 的 GraphQL error 336 路径，将异常交回 worker 的重试
  机制，并在异常退出后安全恢复 ownerless request lock。
- crawl worker 使用 advisory lease；正常停止保留真实 rate-limit lock，只有确认
  上一次非正常退出且没有其他持有者时才清理锁。
- gallery-dl 子进程显式使用 `stdin=DEVNULL`，避免继承失效文件描述符导致
  `Bad file descriptor`。

### 构建与部署

- `docker-compose.build.yml` 成为可跟踪的本地构建模板，复制为本地
  `docker-compose.yml` 后执行构建部署。
- GHCR 镜像固定为：

  ```text
  ghcr.io/inp146/archivex:0.1.2-rc1
  ghcr.io/inp146/archivex-web:0.1.2-rc1
  ```

- Redis、基础镜像和依赖继续使用明确版本或 digest；默认不再发布 `latest` RC
  镜像。
- 前端和 Python 包版本同步更新到 RC1。

## 安装

### 本地构建部署

要求 Docker Engine 和 Docker Compose v2。全新安装使用：

```sh
cp docker-compose.build.yml docker-compose.yml
docker compose up -d --build
```

然后访问 `http://localhost:8000`，使用 Compose 中的 `WEB_AUTH_TOKEN` 登录。
首次对外部署前必须修改示例认证口令；公网部署应使用 HTTPS，并设置
`WEB_COOKIE_SECURE=true`。

### 镜像部署

在独立部署目录下载 RC1 模板：

```sh
curl -fsSL https://raw.githubusercontent.com/INP146/ArchiveX/v0.1.2-rc1/docker-compose.ghcr.yml \
  -o docker-compose.yml
docker compose pull
docker compose up -d
```

镜像部署不需要源码、Python、Node.js 或 `.env`。如果 GHCR 包是私有的，先登录
`ghcr.io`。模板中的 `WEB_AUTH_TOKEN` 仍是示例值，必须替换。首次启动时，Compose
会先运行 `state-migrate`，成功后再启动 API、worker 和 scheduler。

## 从 v0.1.1 迁移

升级前先完成一次可验证备份，并等待任务中心没有 `queued`、`in_progress` 或
`retry_scheduled` 任务。确认旧栈已完全停止后，推荐手动执行一次迁移并检查结果。
使用本地构建模板时：

```sh
docker compose run --rm --build state-migrate
docker compose up --build
```

使用拉取镜像模板时，使用已拉取的镜像：

```sh
docker compose run --rm state-migrate
docker compose up -d
```

手动执行不是硬性要求。使用包含 `state-migrate` 服务的
`docker-compose.build.yml` 时，直接运行 `docker compose up --build` 也会因为
`service_completed_successfully` 依赖自动先执行迁移；迁移完成后再启动 API、worker
和 scheduler。手动执行的价值是把迁移单独拿出来观察，并在启动新服务前确认数据库
完整性。镜像版 Compose 使用同样的依赖关系，只需去掉 `--build`；已经迁移过的环境
会返回 `already_migrated`，可以继续启动。

迁移器会保留旧的 `./data/archive.sqlite3*` 和 `./data/twscrape` 文件，迁移完成
后不要立即删除它们。启动后检查：

```sh
curl -fsS http://127.0.0.1:8000/ready
docker compose run --rm state-migrate
```

第二条命令应返回 `already_migrated`。确认账号、帖子、任务历史和 twscrape 登录
账号都存在后，再按部署环境安排旧目录的清理。

日常停止或重建不要使用 `docker compose down -v`；该命令会删除包含主 SQLite
状态的 `state_data` 和 Redis 数据卷。

## 备份与恢复

停止服务后创建备份：

```sh
docker compose stop
docker compose run --rm tools backup
docker compose up -d
```

备份包含归档 SQLite、twscrape Session 数据库、原始 JSON 和媒体。Redis 只作为
任务传输层，不属于备份主体；任务生命周期已经保存在归档 SQLite 中。

恢复前停止服务，并使用工具校验备份：

```sh
docker compose run --rm tools verify backups/archivex-YYYYMMDDTHHMMSSZ.tar.gz
docker compose run --rm tools restore backups/archivex-YYYYMMDDTHHMMSSZ.tar.gz --replace
```

新布局下不要尝试直接替换 Docker named volume 的 `/data` mountpoint。需要恢复
时先恢复到宿主机 staging 目录，再运行一次 `state-migrate` 原子写入 named volume。

## 已知限制

- 本版本为 RC，尚未承诺稳定版兼容性；升级和回滚必须以备份为前提。
- 部署范围仍是单机 Docker Compose，不支持多节点或 Kubernetes。
- X Cookie 会过期，X 接口变化可能导致抓取失败，需要在 Web 设置页人工轮换。
- crawl worker 默认单并发；提高并发需要多个可用的独立 twscrape 登录账号，并应
  结合平台限流谨慎调整。
- 默认入口是本机 HTTP；公网 TLS、域名和额外访问控制由外部反向代理负责。
- 不内置自动定时备份、异地备份、磁盘容量告警或 Session 失效通知。
- 镜像版 Compose 迁移旧数据时会读取部署目录中的 `./data`，但只将
  `./data/archive` 继续作为媒体目录；SQLite 和 twscrape 状态会迁移到 Docker
  named volume。升级前必须停止旧栈并完成备份。

## 验证记录

本次 RC1 在不启动 API、worker、scheduler、Redis 或 Vite 服务的情况下完成：

```text
.venv/bin/pytest -q                                      130 passed, 1 warning
npm run build                                            passed
docker compose -f docker-compose.ghcr.yml config --quiet passed
git diff --check                                         passed
```

warning 为现有 FastAPI TestClient 对 `httpx` 的弃用提示，不影响测试结果。

## 发布检查清单

- [x] 版本字段、README 和 GHCR Compose 模板更新为 `v0.1.2-rc1`。
- [x] Python 测试、前端生产构建和 Compose 配置解析通过。
- [ ] 推送 `v0.1.2-rc1` tag，并确认 GHCR 后端与 Web 的 `amd64/arm64` 镜像均发布。
- [ ] 在全新目录完成本地构建部署、登录、Cookie 导入和首次同步。
- [ ] 使用旧版数据完成 `state-migrate`，验证账号、帖子、媒体和任务历史。
- [ ] 完成一次备份、校验和恢复演练。
- [ ] 验证 `/ready`、手动同步、定时同步、任务重试、pending 回收和容器重启恢复。
- [ ] 完成桌面与移动端主要导航检查，并更新 `.agents/MOBILE_UI_DESIGN_QA.md`。
- [ ] RC 反馈收集完成后，再决定是否发布 `v0.1.2` 稳定版。
