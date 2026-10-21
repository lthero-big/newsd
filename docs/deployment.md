# 部署指南

English: [deployment.en.md](deployment.en.md)

newsd 由两个进程组成，共用同一个数据库：

- **web**：`newsd serve`，FastAPI，监听 8080。启动时自动跑数据库迁移。
- **worker**：`newsd worker start`，APScheduler，在 `NEWSD_WORKER_TIMES` 采集一次共享池，再给每个用户出期，到点推送。

不需要 Redis、消息队列、GitHub Actions 或 Cloudflare。只要 Python 3.11+（或 Docker）和一个能出网的机器。

## 必填配置

复制 `.env.example` 为 `.env`，**至少**设置：

```bash
NEWSD_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

- `NEWSD_SECRET_KEY` 用来加密用户的模型 key、推送密钥，并签发 session。**设定后不要更换、不要丢**——换了以后已存的密钥全部解不开，用户需要重新填。备份时一并保管。
- HTTPS 之后（见下）把 `NEWSD_COOKIE_SECURE=1`。
- `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ`：每天几点出日报，例如 `07:00` + `Asia/Shanghai`。
- `NEWSD_PUBLIC_URL`：对外访问地址，推送消息里的“查看完整日报”链接用它。
- 其余可选项（SMTP、Telegram 默认 bot、模型网关、推送重试等）见 `.env.example` 里的注释。

## 方式一：docker compose

```bash
git clone https://github.com/lthero-big/newsd.git && cd newsd
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose exec web newsd create-admin admin          # 提示输入密码
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json
docker compose logs -f worker
```

- `web` 和 `worker` 用同一镜像、同一个命名卷 `newsd-data`（挂在 `/data`，SQLite 文件 `/data/newsd.db`）。`worker` 等 `web` 健康检查通过后才启动。
- 端口 `8080` 直接映射到宿主机。放在公网时请只暴露反向代理：在 `.env` 里设 `NEWSD_PORT=127.0.0.1:8080`。
- 立刻手动跑一次完整管线看效果：`docker compose exec worker newsd worker run-once --no-ai`。
- 升级：见下面「镜像与升级」（迁移在 web 启动时自动执行）。

### 镜像

- `Dockerfile` 是多阶段构建：第一阶段在虚拟环境里装好 newsd 和依赖（已包含 Postgres 驱动），运行阶段只复制虚拟环境，不带源码和编译工具；进程以非 root 用户 `newsd`（uid 10001）运行，数据在 `/data`。
- 发布版本（打 `vX.Y.Z` 标签）时，GitHub Actions 会构建 amd64 和 arm64 镜像并推送到 `ghcr.io/lthero-big/newsd`，标签为 `X.Y.Z`、`X.Y` 和 `latest`。推送前会先做一次冒烟测试（能启动、`/healthz` 正常、以 uid 10001 运行）。
- 用已发布的镜像：`docker compose pull && docker compose up -d`（`.env` 里 `NEWSD_VERSION` 可以固定版本，默认 `latest`）。用当前代码自己构建：`docker compose up -d --build`。
- PyPI 慢或不可用时：`docker compose build --build-arg PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple`。

### 镜像与升级

- 手动：`docker compose pull && docker compose up -d`（自己构建的话 `git pull && docker compose up -d --build`）。
- 网页端：启用 `updater` 后在管理概览里点「更新」，见 [updates.md](updates.md)。
- **从旧镜像升级**：以前的镜像以 root 运行，数据卷里的文件属于 root。新镜像启动时如果发现 `/data` 不可写，会退出并打印修复命令，执行一次即可：
  ```bash
  docker compose run --rm --user 0 --entrypoint chown web -R 10001:10001 /data
  ```

### 使用 Postgres

compose 文件里自带一个可选的 Postgres 16（profile `postgres`，数据在卷 `newsd-pg`，不映射端口）。在 `.env` 里加：

```bash
COMPOSE_PROFILES=postgres
NEWSD_PG_PASSWORD=换成一串随机字符
NEWSD_DATABASE_URL=postgresql+psycopg://newsd:同一个密码@db:5432/newsd
```

然后 `docker compose up -d`。web 会等 Postgres 健康后再启动并建表。用外部 Postgres 时不要开这个 profile，直接把 `NEWSD_DATABASE_URL` 指过去。psycopg 是 LGPL-3.0，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 方式二：裸机（systemd）

```bash
sudo useradd -r -m -d /opt/newsd newsd
sudo -u newsd git clone https://github.com/lthero-big/newsd.git /opt/newsd/app
cd /opt/newsd/app
sudo -u newsd python3.11 -m venv .venv          # 3.11 或更新
sudo -u newsd .venv/bin/pip install .            # 要 Postgres 就用 '.[postgres]'
sudo -u newsd cp .env.example .env && sudo -u newsd $EDITOR .env
# .env 里把 NEWSD_DATABASE_URL 指到绝对路径，例如 sqlite:////opt/newsd/data/newsd.db
sudo -u newsd .venv/bin/newsd db upgrade
sudo -u newsd .venv/bin/newsd create-admin admin
sudo -u newsd .venv/bin/newsd db import-config --sources config/collect-sources.json --tags config/tags.json
```

`/etc/systemd/system/newsd-web.service`：

```ini
[Unit]
Description=newsd web
After=network-online.target

[Service]
User=newsd
WorkingDirectory=/opt/newsd/app
ExecStart=/opt/newsd/app/.venv/bin/newsd serve --host 127.0.0.1 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/newsd-worker.service`：同上，`Description=newsd worker`，`ExecStart=/opt/newsd/app/.venv/bin/newsd worker start`，`After=newsd-web.service`。

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now newsd-web newsd-worker
```

`.env` 在 `WorkingDirectory` 里被自动读取（也可用 `NEWSD_ENV_FILE` 指定路径）。

## 反向代理与 HTTPS

web 自己不做 TLS。用 Caddy / nginx 终止 HTTPS 并转发到 `127.0.0.1:8080`，然后在 `.env` 设 `NEWSD_COOKIE_SECURE=1` 和 `NEWSD_PUBLIC_URL=https://你的域名`。Caddy 例：

```
news.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

## 注册方式与第一批用户

默认 `NEWSD_REGISTRATION=invite`：管理员 `newsd admin invite` 生成一次性邀请码发给用户。公开站点可以切到 `open`，不想让人自己注册就用 `closed`。运行中切换：`newsd admin registration open`。

## 备份与恢复

- SQLite：停 worker 后拷贝 `newsd.db`（以及 `-wal`/`-shm`，如果存在），或在线用 `sqlite3 newsd.db ".backup backup.db"`。Docker 卷里的文件：`docker compose exec web sqlite3 /data/newsd.db ".backup /data/backup.db"`（镜像里没有 sqlite3 命令时用 `python -c`）。
- Postgres：`pg_dump`。
- **必须连同 `NEWSD_SECRET_KEY` 一起备份**，两者缺一不可。

## 排障

- `GET /healthz` 返回 200 即 web 正常。
- 管理后台「日志与任务」页和 `newsd worker runs` 看每次管线的时间线、每个用户的结果和错误；同一页的「推送记录」和 `newsd push list` 看投递结果；「源健康」页看各信息源的抓取状态。
- 推送到内网地址的 Webhook 默认被拒绝（SSRF 防护）；确实需要时设 `NEWSD_PUSH_ALLOW_PRIVATE=1`。
- `database is locked`：SQLite 同时被多个进程写时偶发，连接超时已设为 30 秒；写入压力大就换 Postgres。

## Vercel + Supabase

实验性，见 [vercel.md](vercel.md)。
