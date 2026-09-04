# ArchiveX v0.1.2-rc2 发布说明

## 发布目的

`v0.1.2-rc2` 修复 X 在 2026-08-24 调整 Web bundle 哈希格式后，所有账号解析和
时间线抓取均失败的问题。故障时 X 页面仍返回 HTTP 200，但 `twscrape 0.19.2`
无法发现签名脚本并返回空结果，导致 Web 误报 `X account not found`。

## 主要变更

- 将 `twscrape` 从 `0.19.2` 升级并锁定到 `0.20.1`，支持 X 当前使用的 16 位
  bundle 哈希及最新 GraphQL operation IDs。
- 移除仅适用于 `twscrape 0.19.2` 的 GraphQL error 336 进程退出拦截。
- 将 0.20.1 的 `GqlFeaturesOutdatedError` 转换为 ArchiveX 的可重试抓取异常，保留
  Taskiq 自动重试行为。
- 保留 ArchiveX 的请求 lease、异常关闭释放和 ownerless lock 恢复逻辑。
- 包含 `v0.1.2-rc1` 之后 main 分支上的配置校验和身份展示修复。

## 镜像

```text
ghcr.io/inp146/archivex:0.1.2-rc2
ghcr.io/inp146/archivex-web:0.1.2-rc2
```

两个镜像均由 `v0.1.2-rc2` tag 触发 GitHub Actions 构建，并发布
`linux/amd64`、`linux/arm64` manifest。

## 升级

部署前先按现有流程创建并验证备份，然后下载与 tag 匹配的 Compose 文件：

```sh
curl -fsSL https://raw.githubusercontent.com/INP146/ArchiveX/v0.1.2-rc2/docker-compose.ghcr.yml \
  -o ./docker-compose.yml
docker compose pull
docker compose up -d --remove-orphans
docker compose ps
```

本版本不修改 ArchiveX 或 twscrape 数据库 schema，不需要重新导入已有 Cookie，
也不需要执行额外数据迁移。部署后应先验证 `/ready`，再解析一个已知存在的公开账号，
并检查 crawl worker 不再出现 `XClIdParseError: X web scripts not found`。

## 发布验证

- [x] 后端完整测试通过。
- [x] 前端生产构建通过。
- [x] 本地构建版、默认版及 GHCR 版 Compose 配置校验通过。
- [x] Python 依赖锁定到 `twscrape 0.20.1` 且依赖检查无冲突。
- [x] 后端与 Web Docker 镜像构建通过。
- [x] rc2 本地镜像使用生产会话数据库快照成功解析 `@Tesla`。
- [ ] 推送 `main` 和带注释的 `v0.1.2-rc2` tag。
- [ ] GitHub Actions 发布任务成功。
- [ ] GHCR 中两个镜像的 `amd64/arm64` manifest 可用。
