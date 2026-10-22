<div align="center">

# newsd

**可自托管的多用户 AI 日报。** 每个人订阅自己的信息源和关键词，newsd 每天定时采集、去重、打分，生成一份个人日报，并推送到你常用的地方。

[![CI](https://github.com/lthero-big/newsd/actions/workflows/ci.yml/badge.svg)](https://github.com/lthero-big/newsd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776ab)
![DB](https://img.shields.io/badge/db-SQLite%20%7C%20Postgres-336791)

[功能](#功能) · [截图](#截图) · [部署](#部署) · [升级](#升级) · [备份](#备份与恢复) · [配置](#配置) · [常见问题](#常见问题) · [开发](#开发)

</div>

*newsd is a self-hosted, multi-user AI news digest: per-user sources, keywords and quotas, a scheduled daily issue, optional LLM re-ranking / translation / summaries with automatic provider failover, push delivery to Telegram, e-mail and webhooks, and an admin console with user groups, source health and structured logs. Python, FastAPI, SQLite or Postgres. MIT licensed.*

## 功能

**阅读**
- 每天一期个人日报：首屏大号日期、当期导语、按分组的模块标签、日历翻看往期；手机和桌面都能用，内置 14 套主题和明暗切换。
- 卡片上可以点赞、点踩、折叠、删除和收藏；newsd 会从你的反馈里学习，逐渐给不喜欢的内容降权。删除的条目进回收站，30 天后自动清理。
- 收藏、归档搜索、回收站各有独立页面。

**信息源**
- 内置 Hacker News、Reddit、RSS/Atom、GitHub、Hugging Face、arXiv、OSSInsight，以及经自建 RSSHub 接入的 X 等。
- 按类别管理，每个类别下可以建自己的**分区**（如「中文」「英文」「特别关注」），拖动排序、整体开关；「特别关注」分区的源每条必推，不受配额限制。
- 所有用户共享一个采集池，同一个源每轮只抓一次；自动抽取正文和发布时间，内置 SSRF 防护。

**大模型（可选）**
- 支持 OpenAI 兼容接口、Anthropic、Gemini、DeepSeek、通义、豆包、硅基流动、OpenRouter、Ollama 等。
- 每个人可以添加多个 provider 组成**故障切换链**：主用失败自动换备份。key 用服务器主密钥加密存储，页面上只显示后 4 位。
- 不配模型也能正常出日报，只是少了 AI 打分、语义去重、翻译和摘要。

**推送**
- Telegram、SMTP 邮件、Webhook（通用 / 飞书 / 钉钉 / Slack / Discord，支持 `#{变量}` 模板）。
- 失败自动退避重试，有投递记录和一键测试。

**多用户与管理后台**
- 管理员、成员、只读三种角色；邀请码 / 公开 / 关闭三种注册方式，可随时切换；登录失败 3 次锁定 IP 15 分钟。
- **用户分组**：每个组有默认角色、权限、默认订阅源和每日配额，一个人可以属于多个组，权限取并集；支持批量加组、停用、强制下线、重置密码，批量生成绑定分组的邀请链接。
- **源健康**：每个源的失败 / 警告 / 正常状态和 14 天历史，一键重试。
- **日志与任务**：结构化事件日志（抓取、日报、模型切换、推送、登录、管理操作，保留 30 天并自动脱敏），以及每次定时任务的运行时间线。
- 每个用户可以生成 API key，供脚本读取自己的日报。

## 截图

| 每日日报 | 订阅源与分区 |
| --- | --- |
| ![每日日报](docs/screenshots/reading.png) | ![订阅源](docs/screenshots/sources.png) |
| **模型与故障切换** | **管理概览** |
| ![模型](docs/screenshots/models.png) | ![管理概览](docs/screenshots/admin-overview.png) |
| **用户与分组** | **源健康** |
| ![用户与分组](docs/screenshots/admin-users.png) | ![源健康](docs/screenshots/admin-health.png) |
| **日志与任务** | **手机端** |
| ![日志与任务](docs/screenshots/admin-logs.png) | <img src="docs/screenshots/mobile.png" alt="手机端" width="260"> |

## 架构

```mermaid
flowchart LR
  subgraph newsd
    W[web<br/>newsd serve<br/>FastAPI] --- DB[(SQLite / Postgres)]
    S[worker<br/>newsd worker start<br/>定时调度] --- DB
  end
  S -->|采集| SRC[HN · Reddit · RSS · GitHub · HF · arXiv · RSSHub]
  S -->|打分 / 翻译 / 摘要| LLM[用户自己的大模型 provider]
  S -->|推送| PUSH[Telegram · 邮件 · Webhook]
  U[浏览器 / API] --> W
```

- **web**（`newsd serve`）：页面和 API，启动时自动执行数据库迁移。
- **worker**（`newsd worker start`）：在 `NEWSD_WORKER_TIMES` 先采集一次共享池，再逐个用户生成日报，到点推送。
- 两个进程共用同一个数据库，不需要 Redis 或消息队列。数据库默认 SQLite，用户多时换成 Postgres。
- 没有常驻进程的平台（Vercel）上没有 worker：由外部定时器调用受密钥保护的 `/api/cron/tick`，每次调用做 worker 该做的事（到点跑日报、发推送），重复调用不会重复执行。

## 部署

推荐顺序：**Docker Compose**（自己的服务器，功能最完整，已完整验证，包括网页端一键更新）→ **本机直接运行** → **Vercel + Supabase**（免服务器，**实验性**：有平台限制，见下文）。

不管哪种方式，都要先生成一个主密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

> `NEWSD_SECRET_KEY` 用来加密用户保存的模型 key 和推送密钥。**设定后不要更换、不要丢失**：换了以后已保存的密钥都解不开，用户需要重新填写。备份数据库时一定要连同它一起保存。

### 方式一：Docker 一键部署（推荐）

只需要 Docker（含 Docker Compose 插件），不用拉代码，直接用 Docker Hub 上的预构建镜像 [`lthero1/newsd`](https://hub.docker.com/r/lthero1/newsd)（amd64 / arm64）：

```bash
mkdir newsd && cd newsd
curl -fsSL -o docker-compose.yml https://raw.githubusercontent.com/lthero-big/newsd/main/deploy/docker-compose.yml
echo "NEWSD_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n=+/')" > .env   # 主密钥，保存好，不要更换
docker compose up -d                              # 启动 web（端口 8080）和定时 worker
docker compose exec web newsd create-admin admin  # 创建管理员，按提示输入密码
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json   # 导入默认信息源和标签
```

升级：`docker compose pull && docker compose up -d`。其他选项（端口、固定版本、Postgres、网页端更新）写在同目录 `.env` 里，说明见 [deploy/docker-compose.yml](deploy/docker-compose.yml) 开头。

### 方式一（源码版）：Docker Compose 从源码构建

需要 Docker 和 Docker Compose。

```bash
git clone https://github.com/lthero-big/newsd.git
cd newsd
cp .env.example .env        # 至少把 NEWSD_SECRET_KEY 改成上面生成的随机串
docker compose up -d --build                     # 启动 web（端口 8080）和定时 worker
docker compose exec web newsd create-admin admin # 创建管理员，按提示输入密码
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json   # 导入默认信息源和标签
```

浏览器打开 http://localhost:8080 ，用管理员账号登录。数据保存在名为 `newsd-data` 的 Docker 卷里。立刻跑一轮看效果：`docker compose exec worker newsd worker run-once`。

- **镜像**：多阶段构建，以非 root 用户（uid 10001）运行。发布版本后可以直接用 Docker Hub `lthero1/newsd`（同时推送到 `ghcr.io/lthero-big/newsd`）的预构建镜像（amd64 / arm64）：`docker compose pull && docker compose up -d`，不用 `--build`。PyPI 慢时构建可加 `--build-arg PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple`。
- **用 Postgres**：在 `.env` 里加 `COMPOSE_PROFILES=postgres`、`NEWSD_PG_PASSWORD=<随机密码>` 和 `NEWSD_DATABASE_URL=postgresql+psycopg://newsd:<同一个密码>@db:5432/newsd`，compose 会一起启动自带的 Postgres 16。
- **网页端更新**：再加 `--profile updater`，见 [docs/updates.md](docs/updates.md)。
- 端口、反向代理、systemd 等细节见 [部署指南](docs/deployment.md)。

### 方式二：本机直接运行

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

长期运行建议用 systemd 托管 `serve` 和 `worker start` 两个进程，写法见 [部署指南](docs/deployment.md)。

### 方式三：Vercel + Supabase（实验性）

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
- 需要更频繁（按用户推送时间推送、一天多期）：用 **GitHub Actions** 每 10 分钟调用一次（模板 [examples/github-actions/newsd-tick.yml](examples/github-actions/newsd-tick.yml)），或升级 Vercel Pro 把 cron 改成 `*/10 * * * *`。重复调用是安全的：同一个时间点只会跑一次日报。
- **函数时长上限**：Hobby 300 秒，Pro 800 秒。一次日报要在这个时间内完成（采集 + 每个用户的打分/翻译/摘要），信息源和用户多的话可能超时；超时的运行会在下次调用时标记为「中断」。规模大时请用 Docker。
- 不支持网页端一键更新：推送到仓库或在 Vercel 里 Redeploy 即可更新。

完整说明（连接池、迁移、预览环境、GitHub Actions 定时器、本地验证方法）见 [docs/vercel.md](docs/vercel.md)。

### 上线前检查

- **HTTPS**：newsd 本身不处理 TLS。用 Caddy 或 nginx 反向代理到 `127.0.0.1:8080`，并设置 `NEWSD_COOKIE_SECURE=1` 和 `NEWSD_PUBLIC_URL=https://你的域名`。
- **时区和时刻**：`NEWSD_WORKER_TIMES=07:00`、`NEWSD_WORKER_TZ=Asia/Shanghai`。
- **注册方式**：默认只允许邀请码注册，在管理后台「用户与分组」里切换或生成邀请链接。
- **健康检查**：`GET /healthz` 返回 200 和当前版本号。

## 升级

数据库迁移是自动的：web 启动时会把数据库升级到当前版本。**升级前先做一次备份**（见下一节）。

**网页端一键更新**：管理员在「管理 → 概览」的「版本与更新」卡片里能看到有没有新版本和更新说明，点「更新」即可：本机部署会快进代码、自动备份 SQLite、迁移数据库并重启；Docker 部署需要先启用可选的 `updater` 容器（`docker compose --profile updater up -d`），由它拉取新镜像并依次重建 web 和 worker，新 web 没起来会自动回滚。原理和安全说明见 [docs/updates.md](docs/updates.md)。

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

## 备份与恢复

需要备份的只有两样东西，**缺一不可**：

1. **数据库**
   - SQLite（默认）：在线备份 `sqlite3 var/newsd.db ".backup newsd-backup.db"`；Docker 里的文件在卷 `newsd-data` 的 `/data/newsd.db`，可以用
     `docker compose exec web python -c "import sqlite3; s=sqlite3.connect('/data/newsd.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"`
     再 `docker compose cp web:/data/backup.db .` 拷出来。
   - Postgres / Supabase：`pg_dump "$NEWSD_DATABASE_URL" > newsd.sql`（连接串去掉 `+psycopg`）。Supabase 请用 **Session pooler**（端口 5432）或直连地址做备份，不要用 6543 的事务连接池；免费版 Supabase 也有每日自动备份。
2. **`NEWSD_SECRET_KEY`**：存到密码管理器里。没有它，备份里加密保存的模型 key 和推送密钥都无法解密。

恢复：停掉 web 和 worker，把备份放回原位置（或 `psql < newsd.sql`），确认 `.env` 里是**同一个** `NEWSD_SECRET_KEY`，再启动。

## 配置

所有配置都通过环境变量设置，写在 `.env` 里；[.env.example](.env.example) 列出了全部选项和说明。常用的有：

| 变量 | 作用 |
| --- | --- |
| `NEWSD_SECRET_KEY` | **必填**，加密用户密钥和签发会话的主密钥 |
| `NEWSD_DATABASE_URL` | 数据库地址，默认 `sqlite:///./var/newsd.db`；Postgres 用 `postgresql+psycopg://user:pass@host:5432/db` |
| `NEWSD_REGISTRATION` | 默认注册方式：`invite`、`open` 或 `closed`（运行中可在后台切换） |
| `NEWSD_COOKIE_SECURE` | 在 HTTPS 后面时设为 `1` |
| `NEWSD_PUBLIC_URL` | 对外访问地址，推送消息里的「查看完整日报」链接用它 |
| `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ` | 每天出日报的时刻和时区，例如 `07:00` 和 `Asia/Shanghai` |
| `NEWSD_SMTP_*` | 邮件推送用的 SMTP 服务器 |
| `NEWSD_TELEGRAM_BOT_TOKEN` | 可选，给没有填自己 bot 的用户用的默认 Telegram bot |
| `RSSHUB_BASE` / `RSSHUB_ACCESS_KEY` | 自建 RSSHub 地址；不设时 X 等依赖 RSSHub 的源会跳过 |
| `NEWSD_GITHUB_TOKEN` | 可选，提高 GitHub 源和版本检查的接口限额 |
| `NEWSD_UPDATE_CHECK` | 设为 `0` 关闭版本检查；`NEWSD_DEPLOY_MODE=none` 关闭网页端更新 |
| `NEWSD_RESTART_CMD` | 本机部署网页端更新后的重启命令，例如 `sudo systemctl restart newsd-web newsd-worker`；不设时进程自己重启 |

`config/collect-sources.json` 和 `config/tags.json` 是默认的公开信息源目录和标签词表，导入后可以在网页上继续增删。

## 使用

**管理员**
1. 在「管理 → 用户与分组」里选择注册方式，建分组（角色、权限、默认订阅源、每日配额），然后生成邀请链接发给用户。命令行也可以：`newsd admin invite`、`newsd create-user <名字>`、`newsd set-role <名字> user|admin|readonly`。
2. 在「源健康」里看哪些源抓取失败，一键重试或停用。
3. 在「日志与任务」里看每次定时任务的时间线和出错详情，也可以手动运行一次。

**普通用户**
1. 注册后在引导页一键订阅一组预设信息源，之后在「订阅源」里按类别和分区调整。
2. 在「关键词、配额与推送」里设置关注 / 加权 / 屏蔽关键词、各分组条数，以及推送渠道和时间。
3. （可选）在「模型」里添加一个或多个大模型 provider，点「测试」确认可用。
4. 每天到点后在首页看当期日报，用日历翻看往期，收藏或删除条目。

**命令行**（Docker 里在命令前加 `docker compose exec web`）

```bash
newsd worker run-once            # 立即跑一轮：采集 + 给所有用户出日报
newsd worker runs                # 查看最近的运行记录
newsd issue build alice          # 只给 alice 生成今天的日报（--all 所有用户，--no-ai 不调用模型）
newsd push test alice telegram   # 用 alice 保存的配置发一条测试推送
```

**API**：先登录拿到 cookie，再用 `POST /api/tokens` 生成 API key（只显示一次，服务器只存哈希），之后放在 `Authorization: Bearer <key>` 请求头里调用：

```bash
curl -c cookie.txt -d username=alice -d password='你的密码' http://localhost:8080/login
curl -b cookie.txt -X POST -d kind=api -d name=script http://localhost:8080/api/tokens
curl -H "Authorization: Bearer $NEWSD_API_KEY" http://localhost:8080/api/me/issues
```

## 常见问题

**不配大模型能用吗？**
能。日报按关键词规则打分和去重；配上模型后多了 AI 打分、语义去重、翻译和摘要。

**SQLite 还是 Postgres？**
个人和小团队用 SQLite 足够（web 和 worker 共用一个文件，已开启 WAL）。用户多、写入频繁，或者部署在 Vercel 这类无状态平台上时，用 Postgres。

**Vercel 上日报没有按时出 / `/api/cron/tick` 返回 401 或 503？**
503 表示没有设置 `CRON_SECRET`（接口默认关闭）；401 表示调用方带的密钥不对，Vercel Cron 会自动带 `Authorization: Bearer $CRON_SECRET`，GitHub Actions 需要把同一个值存成仓库 Secret。Hobby 版每天只触发一次且有最多 59 分钟的随机延迟；运行超过函数时长上限会被平台中止，在「日志与任务」里显示为「中断」。见 [docs/vercel.md](docs/vercel.md)。

**换了 `NEWSD_SECRET_KEY` 会怎样？**
已保存的模型 key 和推送密钥都无法解密，所有人需要重新填写，已登录的会话也会失效。请把它和数据库一起备份。

**X（Twitter）的源抓不到？**
X 和部分国内源需要自建 RSSHub，设置 `RSSHUB_BASE`（和 `RSSHUB_ACCESS_KEY`）后才会抓取，未设置时这些源会被跳过。

**推送到内网 Webhook 被拒绝？**
出于 SSRF 防护，默认不允许推送到内网和回环地址；确实需要时设置 `NEWSD_PUSH_ALLOW_PRIVATE=1`。

**日报什么时候生成？为什么今天还没有？**
worker 在 `NEWSD_WORKER_TIMES` 指定的时刻（`NEWSD_WORKER_TZ` 时区）运行。确认 worker 进程在跑，然后在「管理 → 日志与任务」看最近一次运行的时间线和错误；也可以在那里手动运行一次。

**Docker 里提示 `/data is not writable`？**
旧版镜像以 root 运行，新镜像改为非 root（uid 10001）。执行一次 `docker compose run --rm --user 0 --entrypoint chown web -R 10001:10001 /data` 修正数据卷的属主。

**出现 `database is locked`？**
SQLite 被多个进程同时写入时偶发，连接已设置 30 秒超时；写入压力大时换成 Postgres。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # 测试全部使用模拟 HTTP，不需要联网
.venv/bin/ruff check .    # 代码检查
```

每次 push 和 Pull Request 都会在 GitHub Actions 上运行 ruff、pytest（Python 3.11 / 3.12）和 gitleaks 密钥扫描。更细的设计文档在 [docs/](docs/)。

## 许可

[MIT](LICENSE)。依赖和参考项目的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
