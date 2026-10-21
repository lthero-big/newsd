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
- 端口 `8080` 直接映射到宿主机。放在公网时请只暴露反向代理，把 compose 里的端口改成 `127.0.0.1:8080:8080`。
- 立刻手动跑一次完整管线看效果：`docker compose exec worker newsd worker run-once --no-ai`。
- 升级：`git pull && docker compose up -d --build`（迁移在 web 启动时自动执行）。

### 使用 Postgres

镜像默认不带 Postgres 驱动。在 `Dockerfile` 里把 `pip install --no-cache-dir .` 改成 `pip install --no-cache-dir '.[postgres]'`，在 compose 里加一个 `postgres` 服务，并设置：

```bash
NEWSD_DATABASE_URL=postgresql+psycopg://newsd:CHANGE_ME@db:5432/newsd
```

同时去掉 compose 里 `environment:` 下对 `NEWSD_DATABASE_URL` 的覆盖。psycopg 是 LGPL-3.0，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

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
- 管理后台的“Worker 运行”页和 `newsd worker runs` 看每次管线的各阶段、每个用户的结果和错误；“推送记录”页和 `newsd push list` 看投递结果。
- 推送到内网地址的 Webhook 默认被拒绝（SSRF 防护）；确实需要时设 `NEWSD_PUSH_ALLOW_PRIVATE=1`。
- `database is locked`：SQLite 同时被多个进程写时偶发，连接超时已设为 30 秒；写入压力大就换 Postgres。
