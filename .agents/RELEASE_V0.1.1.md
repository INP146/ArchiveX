# ArchiveX v0.1.1 稳定版发布说明

## 发布范围

`v0.1.1` 是 ArchiveX 的首个可用稳定版本，正式支持单机 Docker Compose 部署。
本地 Python 与 Vite 启动方式只用于开发，不属于稳定版部署接口。
`v0.1.0` 的 Web 镜像缺少 Nginx 静态文件根目录配置，会使页面返回 500，不能部署。

首版包含：

- 通过 Web 添加和管理归档目标，使用稳定的 X user ID 保存身份关系。
- 定时及手动同步帖子，保存原始 JSON、展示字段与本地媒体。
- 独立 crawl/media worker、Redis 持久队列、去重、超时及自动重试。
- 登录保护、任务中心、搜索、账号详情和移动端导航。
- 在 Web 设置页导入或轮换 X Cookie，并管理账号级 HTTP 代理。
- SQLite 数据迁移、健康检查以及可验证的本地备份/恢复工具。
- 固定 Docker 基础镜像 digest，并使用精确 Python/npm 依赖锁文件构建。

## 安装

要求 Docker Engine 及 Docker Compose v2，并至少预留可容纳归档媒体的磁盘空间。

```sh
docker compose up --build
```

访问 `http://localhost:8000`，使用 `docker-compose.yml` 中的
`WEB_AUTH_TOKEN` 登录。然后在“采集设置”导入 X Cookie，再添加需要归档的
公开账号。Docker 部署不使用开发用 `.env`，也不需要另外安装 Python 或
Node.js。

非本机访问前必须修改 Compose 中的默认认证口令，并放在 HTTPS 反向代理之后，
同时将 `WEB_COOKIE_SECURE` 设为 `true`。整个 `data/` 目录含敏感信息，不能
提交、公开或传给第三方。

仓库内 `docker-compose.yml` 是可直接构建的示例，保留示例口令。
`docker-compose.ghcr.yml` 是只拉取发布镜像的部署模板，不包含 `build`。将后者
作为独立 `deploy` 目录中的 `docker-compose.yml`，修改示例口令即可；它不依赖
源码目录或 `.env`。

## 发布镜像

推送 `v0.1.1` 这类版本 tag 会触发
`.github/workflows/publish-container-images.yml`，向 GitHub Container Registry
发布两个 `linux/amd64`、`linux/arm64` 镜像：

```text
ghcr.io/inp146/archivex:0.1.1
ghcr.io/inp146/archivex-web:0.1.1
```

workflow 会从 GitHub 仓库路径动态生成全小写镜像名，不在 workflow 中硬编码账号。
API、两个 worker、scheduler 和备份工具使用第一个镜像，Web 使用第二个镜像。
首次发布后需要在 GitHub Packages 设置中确认镜像可见性。部署文件应固定到明确
版本，不依赖 `latest`。

```sh
docker compose pull
docker compose up -d
```

## 备份

`data/` 包含归档 SQLite、twscrape Session 数据库、原始 JSON 和媒体文件。
Redis 队列数据位于 Compose 管理的 `redis_data` 卷；它只是任务传输层，不进入
备份，任务生命周期记录保存在归档 SQLite 中。

为了得到静止的一致快照，先停止 Compose 服务：

```sh
docker compose stop
docker compose run --rm tools backup
docker compose up -d
```

命令在 `backups/` 生成带 UTC 时间戳的压缩包，并在完成前自动执行数据库
完整性检查。已有备份可以单独验证：

```sh
docker compose run --rm tools verify backups/archivex-YYYYMMDDTHHMMSSZ.tar.gz
```

## 恢复

恢复会重建 `data/`，同时删除旧 Redis 卷，避免恢复后的数据库收到旧任务。

```sh
docker compose down -v
docker compose run --rm tools restore backups/archivex-YYYYMMDDTHHMMSSZ.tar.gz --replace
docker compose up --build -d
docker compose ps
```

`--replace` 不会删除原目录，而是先将其改名为
`data.pre-restore-<UTC timestamp>`。确认恢复完成后再人工处理该目录。
恢复生成的数据会移除权限标记，下一次后端容器启动时由镜像 entrypoint 重新
设置容器 UID `10001` 的写入权限，然后降权运行应用。

## 升级与回滚

升级前先生成并验证备份，然后在独立 `deploy` 目录中把两个镜像 tag 改为目标
版本并拉取：

```sh
docker compose pull
docker compose up -d --remove-orphans
docker compose ps
```

若升级后需要回滚，先停止服务，恢复升级前备份，再切回原 tag 并重新构建。
不要只降级代码而继续使用已由新版本迁移过的数据库。

## 已知限制

- 稳定部署范围仅为单机 Docker Compose，不支持多节点或 Kubernetes。
- X Cookie 会过期，也可能因 X 接口变化而失效，需要在 Web 设置页人工轮换。
- 默认入口是本机 HTTP；公网 TLS、域名和访问控制由外部反向代理负责。
- 不内置自动定时备份、异地备份、磁盘容量告警或 Session 失效通知。
- SQLite 与媒体位于同一个本地 `data/` 根目录，恢复以整套数据为单位。

## 发布检查清单

- [ ] 工作区干净，`main` 已推送。
- [ ] `pytest -q` 全部通过。
- [ ] `npm run build` 通过。
- [ ] `docker compose config --quiet` 通过。
- [ ] `docker compose build` 通过。
- [ ] 推送版本 tag 后，GHCR 中两个架构的后端、Web 镜像均发布成功。
- [ ] 在全新检出的仓库中只用 Docker Compose 完成首次启动、登录、Cookie 导入和首次同步。
- [ ] 验证手动同步、定时同步、媒体展示、任务重试及容器重启恢复。
- [ ] 验证桌面与移动端主要导航，完成 `.agents/MOBILE_UI_DESIGN_QA.md`。
- [ ] 创建并验证一次备份，再恢复到独立目录。
- [ ] 确认 README、MIT License、已知限制和安全说明准确。
- [ ] 创建并推送带注释的 `v0.1.1` tag，确认镜像发布后再发布本文件中的版本说明。
