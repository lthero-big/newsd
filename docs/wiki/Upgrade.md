# 升级

数据库迁移是自动的：web 启动时会把数据库升级到当前版本。**升级前先做一次备份**（见 [备份与恢复](Backup)）。

**网页端一键更新**：管理员在「管理 → 概览」的「版本与更新」卡片里能看到有没有新版本和更新说明，点「更新」即可：本机部署会快进代码、自动备份 SQLite、迁移数据库并重启；Docker 部署需要先启用可选的 `updater` 容器（`docker compose --profile updater up -d`），由它拉取新镜像并依次重建 web 和 worker，新 web 没起来会自动回滚。原理和安全说明见 [docs/updates.md](https://github.com/lthero-big/newsd/blob/main/docs/updates.md)。

手动升级：

- **Docker Compose**：预构建镜像 `docker compose pull && docker compose up -d`；自己构建 `git pull && docker compose up -d --build`。
  > 从以前的镜像升级：旧镜像以 root 运行，新镜像以 uid 10001 运行。如果启动时提示 `/data is not writable`，执行一次
  > `docker compose run --rm --user 0 --entrypoint chown web -R 10001:10001 /data` 即可。
- **Vercel**：推送到你的 fork（或在 Vercel 里 Redeploy），生产环境构建时自动迁移数据库。
- **本机运行**：
  ```bash
  git pull
  .venv/bin/pip install -e .
  .venv/bin/newsd db upgrade
  # 然后重启 serve 和 worker 两个进程（systemd：sudo systemctl restart newsd-web newsd-worker）
  ```

当前版本号可以在 `/healthz` 里看到。版本说明见 [GitHub Releases](https://github.com/lthero-big/newsd/releases)。
