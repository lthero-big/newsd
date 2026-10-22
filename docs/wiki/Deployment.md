# 部署

推荐顺序：**Docker Compose**（自己的服务器，功能最完整，已完整验证，包括网页端一键更新）→ **本机直接运行** → **Vercel + Supabase**（免服务器，**实验性**：有平台限制，见下文）。

不管哪种方式，都要先生成一个主密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

> `NEWSD_SECRET_KEY` 用来加密用户保存的模型 key 和推送密钥。**设定后不要更换、不要丢失**：换了以后已保存的密钥都解不开，用户需要重新填写。备份数据库时一定要连同它一起保存。

## 方式一：Docker Compose（推荐）

只需要 Docker（含 Compose 插件）。用 Docker Hub 上的预构建镜像 [`lthero1/newsd`](https://hub.docker.com/r/lthero1/newsd)（amd64 / arm64），不用自己构建。[`docker-compose.yml`](https://github.com/lthero-big/newsd/blob/main/docker-compose.yml) 就在仓库根目录，镜像里也带了一份，不需要再单独下载。

**A. 克隆仓库（推荐）**

```bash
git clone https://github.com/lthero-big/newsd.git && cd newsd
echo "NEWSD_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n=+/')" > .env   # 主密钥，保存好，不要更换
docker compose pull && docker compose up -d       # 拉预构建镜像，启动 web（端口 8080）和定时 worker
docker compose exec web newsd create-admin admin  # 创建管理员，按提示输入密码
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json   # 导入默认信息源和标签
```

**B. 不克隆仓库**：从镜像里取出同一份 compose 文件，其余步骤和 A 一样（需要 0.1.1 及以后的镜像）：

```bash
mkdir newsd && cd newsd
docker run --rm lthero1/newsd cat /app/docker-compose.yml > docker-compose.yml
echo "NEWSD_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n=+/')" > .env
docker compose up -d
docker compose exec web newsd create-admin admin
```

浏览器打开 http://localhost:8080 ，用管理员账号登录。数据保存在名为 `newsd-data` 的 Docker 卷里。立刻跑一轮看效果：`docker compose exec worker newsd worker run-once`。

其他选项都写在同目录的 `.env` 里（完整列表见 [.env.example](https://github.com/lthero-big/newsd/blob/main/.env.example)，常用的在 `docker-compose.yml` 开头）：

- **端口 / 反向代理**：`NEWSD_PORT=127.0.0.1:8080` 只监听本机，交给 Nginx / Caddy 转发；在 HTTPS 后面再加 `NEWSD_COOKIE_SECURE=1`。
- **固定版本**：`NEWSD_VERSION=0.1.0`（默认 `latest`）。
- **用 Postgres**：加 `COMPOSE_PROFILES=postgres`、`NEWSD_PG_PASSWORD=<随机密码>` 和 `NEWSD_DATABASE_URL=postgresql+psycopg://newsd:<同一个密码>@db:5432/newsd`，compose 会一起启动自带的 Postgres 16。
- **网页端更新**：`COMPOSE_PROFILES=updater`（两个都要就写 `postgres,updater`），见 [docs/updates.md](https://github.com/lthero-big/newsd/blob/main/docs/updates.md)。
- **从源码构建**：在克隆的目录里 `docker compose up -d --build`；PyPI 慢时先 `docker compose build --build-arg PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple`。
- 镜像以非 root 用户（uid 10001）运行，同时发布到 `ghcr.io/lthero-big/newsd`。端口、反向代理、systemd 等细节见 [部署指南](https://github.com/lthero-big/newsd/blob/main/docs/deployment.md)。

> Docker 部署不要直接 `cp .env.example .env`：如果 `.env` 里写了 `NEWSD_DATABASE_URL=sqlite:///./var/...`，数据库会落到容器里不可写的目录。只写需要的变量即可。

## 方式二：本机直接运行

需要 Python 3.11 或 3.12。

```bash
git clone https://github.com/lthero-big/newsd.git
cd newsd
cp .env.example .env                 # 同样先设置 NEWSD_SECRET_KEY
python3 -m venv .venv
.venv/bin/pip install -e .           # 已包含 Postgres 驱动
.venv/bin/newsd db upgrade           # 建表 / 迁移
.venv/bin/newsd create-admin admin   # 管理员；普通账号用 newsd create-user <名字>
.venv/bin/newsd db import-config --sources config/collect-sources.json --tags config/tags.json
.venv/bin/newsd serve                # Web 服务：http://localhost:8080
.venv/bin/newsd worker start         # 另开一个终端：定时采集、出日报、推送
```

长期运行建议用 systemd 托管 `serve` 和 `worker start` 两个进程，写法见 [部署指南](https://github.com/lthero-big/newsd/blob/main/docs/deployment.md)。

## 方式三：Vercel + Supabase（实验性）

不需要自己的服务器：Vercel 运行 Web（一个 Serverless 函数），Supabase 提供 Postgres，定时任务由 Vercel Cron 或 GitHub Actions 调用 `/api/cron/tick`。

> ⚠️ **实验性**：入口、构建时迁移和定时接口已经在本地用 Postgres + PgBouncer（事务模式，模拟 Supabase 连接池）完整跑通，但**还没有在真实的 Vercel / Supabase 账号上验证过**。遇到问题请提 Issue。

1. Supabase 新建项目，点 **Connect**，复制 **Transaction pooler** 连接串（端口 `6543`，用户名形如 `postgres.<项目ref>`）。
2. Fork 本仓库，在 Vercel 里 **Add New → Project** 导入。框架会自动识别为 FastAPI（入口 `newsd.vercel:app`，见 `pyproject.toml` 和 `vercel.json`）。
3. **Settings → Environment Variables**（只勾 **Production**）：

   | 变量 | 值 |
   | --- | --- |
   | `NEWSD_SECRET_KEY` | 上面生成的随机串 |
   | `NEWSD_DATABASE_URL` | 第 1 步的连接串（`postgresql://...:6543/postgres`） |
   | `CRON_SECRET` | 另一个随机串（至少 16 位），保护 `/api/cron/tick` |
   | `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ` | 例如 `07:00` / `Asia/Shanghai` |

4. 部署。构建时会自动把数据库迁移到最新版本（迁移失败则构建失败，旧版本继续服务）。
5. 创建管理员：本机执行 `NEWSD_DATABASE_URL='<同一个连接串>' NEWSD_SECRET_KEY='<同一个主密钥>' newsd create-admin admin`（`pip install -e .` 之后）。

**限制，请先看清楚：**

- **Hobby（免费版）的 Cron 每天只能触发一次**，而且在设定的那个小时内随机触发（`0 7 * * *` 可能在 07:00–07:59 之间）。`vercel.json` 默认 `0 7 * * *`（UTC），要和 `NEWSD_WORKER_TIMES` 换算成 UTC 后的小时一致。只用 Vercel Cron 时，用户各自的推送时间不生效，推送会在每天这一次运行后统一发出。
- 需要更频繁（按用户推送时间推送、一天多期）：用 **GitHub Actions** 每 10 分钟调用一次（模板 [examples/github-actions/newsd-tick.yml](https://github.com/lthero-big/newsd/blob/main/examples/github-actions/newsd-tick.yml)），或升级 Vercel Pro 把 cron 改成 `*/10 * * * *`。重复调用是安全的：同一个时间点只会跑一次日报。
- **函数时长上限**：Hobby 300 秒，Pro 800 秒。一次日报要在这个时间内完成（采集 + 每个用户的打分/翻译/摘要），信息源和用户多的话可能超时；超时的运行会在下次调用时标记为「中断」。规模大时请用 Docker。
- 不支持网页端一键更新：推送到仓库或在 Vercel 里 Redeploy 即可更新。

完整说明（连接池、迁移、预览环境、GitHub Actions 定时器、本地验证方法）见 [docs/vercel.md](https://github.com/lthero-big/newsd/blob/main/docs/vercel.md)。

## 上线前检查

- **HTTPS**：newsd 本身不处理 TLS。用 Caddy 或 nginx 反向代理到 `127.0.0.1:8080`，并设置 `NEWSD_COOKIE_SECURE=1` 和 `NEWSD_PUBLIC_URL=https://你的域名`。
- **时区和时刻**：`NEWSD_WORKER_TIMES=07:00`、`NEWSD_WORKER_TZ=Asia/Shanghai`。
- **注册方式**：默认只允许邀请码注册，在管理后台「用户与分组」里切换或生成邀请链接。
- **健康检查**：`GET /healthz` 返回 200 和当前版本号。
