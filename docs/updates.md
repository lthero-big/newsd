# 版本检查与网页端更新

管理员在「管理 → 概览」顶部的 **版本与更新** 卡片里能看到当前版本、最新发布版本和更新说明，并可以一键更新。

## 版本检查

- newsd 每 6 小时最多检查一次 GitHub Releases（`NEWSD_UPDATE_REPO`，默认 `lthero-big/newsd`）的最新正式版本（不含草稿和预发布），结果缓存在数据库里，也可以点「检查更新」立即检查。
- 只请求 `api.github.com` 的公开接口，不上报任何数据。设置了 `NEWSD_GITHUB_TOKEN` 时会带上它，以提高接口限额。
- 关闭版本检查：`NEWSD_UPDATE_CHECK=0`。内网镜像或测试时可以用 `NEWSD_UPDATE_URL` 指向一个返回同样 JSON 的地址。
- 命令行：`newsd version` 打印当前版本和最新版本。

发布约定：Release 的标签是 `vX.Y.Z`，并且和 `newsd/__init__.py` 里的 `__version__` 一致；对应的 Docker 镜像标签是 `X.Y.Z`。

## 更新是怎么做的

更新方式按部署方式自动判断（也可以用 `NEWSD_DEPLOY_MODE=docker|local|vercel|none` 指定，`none` 表示关闭网页端更新）。

### Docker Compose：updater 容器

网页端更新需要启用一个可选的 `updater` 容器：

```bash
docker compose --profile updater up -d
```

- **只有 updater 挂载 Docker socket**，web 和 worker 都不挂；updater 不映射任何端口，只能在 compose 内部网络里访问，请求需要带上由 `NEWSD_SECRET_KEY` 派生的令牌。
- updater 只做一件事：拉取 `NEWSD_IMAGE:<版本>`（默认 `ghcr.io/lthero-big/newsd`），然后**只**重建它所在的 compose 项目里 `NEWSD_UPDATER_SERVICES`（默认 `web,worker`）这些服务的容器，先 web、后 worker。新容器沿用原来的环境变量、数据卷、端口、网络、标签、重启策略和健康检查，只换镜像。
- 新的 web 容器启动时会自动执行数据库迁移（每次启动都会），所以「拉取 + 重建」就是完整的升级。
- **自动回滚**：新的 web 容器没有通过健康检查时，会删掉新容器、把旧容器改回原名重新启动，worker 不会被动到。
- updater 本身不会被更新；需要时手动 `docker compose pull updater && docker compose --profile updater up -d updater`。
- `docker-compose.yml` 里的镜像标签默认是 `latest`。如果你在 `.env` 里用 `NEWSD_VERSION` 固定了版本，网页端更新后请把它也改成新版本，否则下次 `docker compose up -d` 会切回旧版本。

> 安全提示：能访问 Docker socket 的容器等同于宿主机的 root 权限。不需要网页端更新就不要启用 `updater`，按下面的手动方式升级即可：
> `docker compose pull && docker compose up -d`

升级前请先备份数据卷里的数据库（见 README「备份与恢复」）。

### 本机部署（git 检出）

点「更新」后，web 进程会：

1. 确认代码目录是 git 检出、没有未提交的修改，并且远端（`NEWSD_UPDATE_REMOTE`，默认 `origin`）有对应的标签 `vX.Y.Z`；
2. 只在能**快进**时把代码快进到这个标签（有本地提交时拒绝，避免覆盖）；
3. 如果 `pyproject.toml` 的依赖有变化，用 pip（没有 pip 时用 uv）重新安装；
4. SQLite 数据库自动备份为 `newsd.db.bak-update-<旧版本>-<时间>`（权限 600）；Postgres 请提前自己 `pg_dump`；
5. 用新代码执行 `newsd db upgrade`，迁移失败时代码退回原来的提交；
6. 重启：设置了 `NEWSD_RESTART_CMD`（例如 `sudo systemctl restart newsd-web newsd-worker`）就执行它，否则 web 进程原地重新启动自己，worker 在一分钟内发现磁盘上的版本变了，也会重新启动自己。

页面会显示每一步的进度；重启完成后显示「更新完成」。5 分钟内没有以新版本重启会显示失败，这时请检查进程管理方式。

### Vercel

不需要在网页里更新：推送到你的 fork，或在 Vercel 控制台点 Redeploy。生产环境构建时会先迁移数据库（`newsd.vercel_build`），迁移失败则构建失败、线上保持旧版本。概览页仍会显示有没有新版本（由 `/api/cron/tick` 定期检查）。见 [vercel.md](vercel.md)。


## 记录

每次更新的开始、完成或失败都会写进「日志与任务」（模块：管理），包括执行人和每一步的结果。
