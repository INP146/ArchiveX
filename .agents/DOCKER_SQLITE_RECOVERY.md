# Docker SQLite SIGBUS 修复与迁移

## 现场结论

2026-08-21 的 47 账号同步并不是 X 抓取持续变慢。代理恢复后，健康任务平均约
1.2 至 1.9 秒完成；异常阶段是 crawl worker 在 SQLite 操作中连续发生 7 次
`SIGBUS`，API 同时出现一次 `database disk image is malformed`。

原 Compose 将整个 `./data` 绑定到 `/data`。API、crawl worker、media worker 和
scheduler 因此在 macOS Docker Desktop 文件共享层上共同使用 SQLite WAL、SHM 和
mmap。该文件共享层不适合作为多进程 SQLite WAL 的主状态盘。worker 子进程崩溃
后，19 条 Redis Stream 消息留在已死亡 consumer 的 PEL 中；旧配置要等待
31 分钟才会 `XAUTOCLAIM`，所以界面看起来像完全停止。

该批任务已于北京时间 2026-08-21 22:51:01 自动恢复并完成，最终 crawl
`pending=0`、`lag=0`，本轮 50 条任务全部 `completed`。

## 永久布局

开发 Compose 使用两个独立持久层：

```text
state_data (Docker Linux named volume)
  /data/archive.sqlite3
  /data/twscrape/accounts.db

./data/archive (macOS host bind mount)
  图片、视频和原始归档文件
```

媒体仍可直接在宿主机访问，但 SQLite WAL、SHM 和锁全部留在 Docker Linux VM
文件系统。应用内路径没有变化，因此不需要运行时兼容分支。
镜像入口只会校正 SQLite/会话文件和媒体根目录的所有权，不会递归 `chown`
整个媒体树；23GB 归档不会再阻塞容器启动。

worker 还会按具体 Redis consumer 写 30 秒 TTL 心跳。心跳只用于健康检查和报告
orphan pending，不会单独触发 40 秒抢占：同一个任务可能仍在执行，过早接管会造成
重复抓取。消息仍由 `XAUTOCLAIM` 在对应任务硬超时加 60 秒保护期后接管；`/ready`
会立即报告 orphan pending，因此不会把这段恢复等待伪装成健康。

## 首次迁移

当前栈由用户手动管理。迁移前先等待任务中心没有 queued、in_progress 或
retry_scheduled，然后在运行 `docker compose up --build` 的终端按 `Ctrl+C`。
不要在旧 worker 仍写库时从第二个终端启动新布局。

停止后执行：

```sh
docker compose run --rm --build state-migrate
docker compose up --build
```

`state-migrate` 以 root 读取可能是 `0700/0600` 的旧目录，但旧 `./data` 仍以只读
方式挂载；迁移器先把数据库及其 WAL/SHM/journal 伴随文件复制到 named volume
内的临时可写目录，再使用 Python SQLite backup API 生成一致快照，执行
`PRAGMA integrity_check` 后才原子替换目标文件。
它不会复制 `-wal`、`-shm` 或 twscrape 进程锁，也不会修改或删除旧文件。再次运行
会验证迁移标记和数据库完整性后返回 `already_migrated`。

启动后检查：

```sh
docker compose ps
curl -fsS http://127.0.0.1:8400/ready
docker compose run --rm state-migrate
```

最后一条应显示 `already_migrated`。确认账号、帖子、任务历史和采集账号都存在后，
旧 `./data/archive.sqlite3*` 与 `./data/twscrape` 仍应保留一段时间，不要手工删除
WAL/SHM 文件。

## 回滚与数据安全

在验证新栈前，回滚只需停止新栈并切回旧 Compose/代码；旧 bind-mounted SQLite
没有被迁移程序改动。新栈开始产生数据后，旧库会落后，不能再把它当作最新副本。

不要运行：

```sh
docker compose down -v
```

`-v` 会删除 `state_data` 和 `redis_data`。普通停止或重建使用 `Ctrl+C`、
`docker compose stop`、`docker compose down` 或 `docker compose up --build`，均不带
`-v`。备份继续使用 `docker compose run --rm tools backup`；工具同时挂载 named
volume 和宿主机媒体目录，并通过 SQLite backup API 创建数据库快照。

`archivex-data restore` 不会尝试替换 Docker 的 `/data` mountpoint（那会得到
`EBUSY`）。需要恢复时，先在宿主机的停止状态目录执行 restore，再确认目录完整后
运行一次 `state-migrate`；这样 named volume 仍由迁移服务原子写入，旧目录也保留为
回滚副本。
