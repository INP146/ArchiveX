# ArchiveX v0.1.2-rc3

rc3 使用帖子归档模型 v3，将 original/reply/quote 内容与 repost 事件分开保存。
多个转发共享 origin 和媒体记录，避免同一原帖按转发次数重复下载。外部作者不会
自动加入主动归档账号，quote 的外层与引用媒体分别展示。

Python 包版本为 `0.1.2rc3`，Web 与容器版本为 `0.1.2-rc3`：

```text
ghcr.io/inp146/archivex:0.1.2-rc3
ghcr.io/inp146/archivex-web:0.1.2-rc3
```

## 升级要求

这是 SQLite schema v2 → v3 的离线迁移。先停止 scheduler，确认 crawl/media
队列的 pending/lag 和数据库活动任务均为 0，再停止 API 和两个 worker。
发布、拉取镜像不等于完成迁移；旧库未迁移时新应用会拒绝启动。
不要使用 `docker compose down -v`，也不要清空整个 Redis。

保持本地 Compose 中的认证信息、端口、路径和其他配置，仅升级后端及 Web 镜像
标签。SQLite 和 twscrape 会话继续保留在 Docker Linux named volume；媒体仍位于
宿主机归档盘。容器启动与停止由部署操作者在升级期间统一执行。

## 空间充足：完整备份迁移

使用 `python -m archivex.migrate` 演练，审查 `report.json` 后执行
`--resume-from`。完整步骤见 [模型实现说明](POST_ARCHIVE_MODEL_IMPLEMENTATION.md)。
该方式会复制整个归档，磁盘已满时不要使用。

## 空间不足：保留媒体、原地移动

`archivex.migrate_compact` 将数据库和所有入库原始 JSON 备份到指定输出目录，
逐文件验证媒体 SHA-256；准备阶段不复制媒体、不修改源文件。输出目录应放在
有空闲空间的另一块磁盘。它适用于 ExFAT，不依赖硬链接或文件系统克隆。

停止所有写入后，以可访问 SQLite、媒体盘及备份盘的维护环境执行：

```sh
python -m archivex.migrate_compact \
  --database /data/archive.sqlite3 \
  --archive-dir /data/archive \
  --session-database /data/twscrape/accounts.db \
  --output-dir /maintenance/post-model-v3
```

检查报告为 `ready`、`issues` 为空，且媒体及观察关系映射完整后应用：

```sh
python -m archivex.migrate_compact --resume-from /maintenance/post-model-v3
```

应用过程：

1. 复核数据库、JSON 备份及候选库；媒体大小与修改时间必须与哈希核验时一致。
2. 删除已完整备份的旧 JSON，为满盘腾出目录空间。
3. 在同一归档文件系统内重命名保留的媒体文件，避免第二份大文件副本。
4. 只有确认保留文件有效且 SHA-256 相同后，才删除对应重复文件。不同字节的
   备用文件以及未入库文件保持原样。
5. 原子安装新的 JSON，验证数据库引用路径、计数、完整性及外键，再切换数据库。

中断后使用同一个 `--resume-from` 继续；不要在迁移未完成时启动 worker。
若维护环境处理的是正式库的一致快照，最后还需在服务保持停止时，用 SQLite
backup API 将校验后的数据库安装回 named volume，保持应用用户拥有文件权限。
不能直接覆盖数据库文件并遗留旧 WAL/SHM。

此模式没有独立的全量媒体备份：回退和恢复依赖归档盘上保留的媒体。因此必须
保留输出目录，并在任何新写入前完成结果核验。需要回退且没有新数据库写入时：

```sh
python -m archivex.migrate_compact --rollback-from /maintenance/post-model-v3
```

回退恢复 v2 业务记录和原始 JSON，并将旧媒体记录指向保留的文件，不重新创建
被去重的媒体副本；随后使用 rc2 应用。若已有新数据库写入，回退会拒绝执行。

## 其他修复与验证

- 迁移恢复入口拒绝未完成的候选库，并检查活动队列任务。
- 完整备份模式的文件安装使用哈希校验后的原子替换，支持复制中断恢复。
- 媒体筛选只展开实际展示的 quote 引用，不包含 reply 目标的隐藏媒体。
- 回复目标只有用户名时保留展示回退，不污染共享的未知作者身份。
- 回归测试覆盖迁移中断、重复文件删除后的续迁、低空间回退、历史任务保留及
  schema/API 行为；174 项后端测试、前端生产构建、依赖检查，以及构建版和
  GHCR 版 Compose 配置检查均通过。
