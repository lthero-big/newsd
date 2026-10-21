# newsd

[![CI](https://github.com/lthero-big/newsd/actions/workflows/ci.yml/badge.svg)](https://github.com/lthero-big/newsd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**可自托管的多用户 AI 日报服务。** 每个用户订阅自己关心的信息源和关键词，newsd 每天定时采集、去重、打分、分组，生成一份个人日报；配上自己的大模型 key 还能做 AI 重排、翻译和摘要，并推送到 Telegram、邮件、飞书、钉钉、Slack、Discord 或任意 Webhook。

*newsd is a self-hosted, multi-user AI news digest: per-user sources, keywords and quotas, a scheduled daily report, optional LLM re-ranking / translation / summaries, and push delivery to Telegram, e-mail and webhooks. Python, FastAPI, SQLite or Postgres. MIT licensed.*

<!-- 截图：放到 docs/images/ 后在这里引用 -->

## 功能

- **多用户**：管理员 / 普通用户两种角色；邀请码注册、公开注册、关闭注册三种模式，可随时切换；登录失败 3 次锁定 IP 15 分钟；每个用户可生成 API key 供脚本调用。
- **信息源**：Hacker News、Reddit、RSS/Atom、GitHub、Hugging Face、OSSInsight，以及经自建 RSSHub 接入的 X 等。所有用户共享一个采集池，同一个源每轮只抓一次；自动抽取正文和发布时间；内置 SSRF 防护。
- **个人日报**：按关键词（关注 / 加权 / 屏蔽）打分，按链接和标题相似度去重，再按话题、分组和总量配额裁剪，每期结果存档。
- **大模型（可选）**：支持 OpenAI 兼容接口、Anthropic、Gemini、DeepSeek、通义、豆包、Ollama。用户自己的 key 加密存储；不配 key 也能正常出日报。
- **定时任务**：内置调度器，按设定的时区和时刻先采集、再给每个用户出日报，每次运行的记录可在管理后台查看。
- **推送**：Telegram、SMTP 邮件、Webhook（通用 / 飞书 / 钉钉 / Slack / Discord，支持 `#{变量}` 模板），失败自动退避重试，有投递记录和一键测试。
- **Web 界面**：日报、日历、收藏、回收站、个人设置（订阅、关键词、配额、推送、模型），以及管理后台（用户、邀请码、注册方式、信息源目录与质量统计、运行记录、推送记录）。

## 部署

### 方式一：Docker Compose（推荐）

需要 Docker 和 Docker Compose。

```bash
git clone https://github.com/lthero-big/newsd.git
cd newsd
cp .env.example .env
```

编辑 `.env`，至少把 `NEWSD_SECRET_KEY` 改成一串长随机字符：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

> `NEWSD_SECRET_KEY` 用来加密用户保存的模型 key 和推送密钥，**设定后不要再改**，备份数据库时也要一起备份它。

启动并初始化：

```bash
docker compose up -d --build                     # 启动 web（端口 8080）和定时 worker
docker compose exec web newsd admin create admin # 创建管理员账号，按提示输入密码
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json   # 导入默认信息源和标签
```

浏览器打开 http://localhost:8080 ，用刚才的管理员账号登录。数据保存在名为 `newsd-data` 的 Docker 卷里。

### 方式二：直接运行（不用 Docker）

需要 Python 3.11 或 3.12。

```bash
git clone https://github.com/lthero-big/newsd.git
cd newsd
cp .env.example .env                 # 同样先设置 NEWSD_SECRET_KEY
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/newsd db upgrade           # 建表
.venv/bin/newsd admin create admin
.venv/bin/newsd db import-config
.venv/bin/newsd serve                # Web 服务：http://localhost:8080
.venv/bin/newsd worker start         # 另开一个终端：定时采集、出日报、推送
```

长期运行建议用 systemd 托管 `serve` 和 `worker start` 两个进程，具体写法见部署文档。

### 生产环境

- **HTTPS**：newsd 本身不处理 TLS，请用 Caddy 或 nginx 反向代理到 `127.0.0.1:8080`，并在 `.env` 设置 `NEWSD_COOKIE_SECURE=1` 和 `NEWSD_PUBLIC_URL=https://你的域名`。
- **数据库**：默认 SQLite，用户多或写入频繁时可以把 `NEWSD_DATABASE_URL` 换成 Postgres（`postgresql+psycopg://...`）。
- **备份**：备份数据库文件（或 `pg_dump`），并且务必连同 `NEWSD_SECRET_KEY` 一起保存。
- **健康检查**：`GET /healthz` 返回 200 表示服务正常。

完整说明（Postgres、systemd、反向代理、备份恢复、排障）见 [部署指南](docs/deployment.md) / [Deployment guide](docs/deployment.en.md)。

## 配置

所有配置都通过环境变量设置，写在 `.env` 里；[.env.example](.env.example) 列出了全部选项和说明。常用的有：

| 变量 | 作用 |
| --- | --- |
| `NEWSD_SECRET_KEY` | 必填，加密用户密钥用的主密钥 |
| `NEWSD_DATABASE_URL` | 数据库地址，默认 `sqlite:///./var/newsd.db` |
| `NEWSD_REGISTRATION` | 默认注册方式：`invite`、`open` 或 `closed` |
| `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ` | 每天出日报的时刻和时区，例如 `07:00` 和 `Asia/Shanghai` |
| `NEWSD_SMTP_*` | 邮件推送用的 SMTP 服务器 |
| `NEWSD_TELEGRAM_BOT_TOKEN` | 可选，给没填自己 bot 的用户用的默认 Telegram bot |
| `RSSHUB_BASE` / `RSSHUB_ACCESS_KEY` | 自建 RSSHub 地址；不设时 X 等依赖 RSSHub 的源会跳过 |
| `NEWSD_GITHUB_TOKEN` | 可选，提高 GitHub 源的接口限额 |

`config/collect-sources.json` 和 `config/tags.json` 是默认的公开信息源目录和标签词表，导入后可以在管理后台和设置页里继续增删。

## 使用

### 管理员

1. 决定谁能注册：
   - `invite`（默认）：用 `newsd admin invite` 生成一次性邀请码发给用户；
   - `open`：任何人都可以在 `/register` 注册；
   - `closed`：只能由管理员在命令行用 `newsd admin create` 建账号。

   运行中切换用 `newsd admin registration open`（不带参数查看当前模式），或在管理后台里改。
2. 在管理后台 `/admin` 管理用户和邀请码、维护信息源目录，查看每次定时任务和推送的记录。

### 普通用户

1. 注册登录后，可以在引导页一键订阅一组预设信息源。
2. 在 **设置** 页调整订阅源、关注 / 加权 / 屏蔽关键词、各分组的条数配额和配色。
3. （可选）在设置页填入大模型服务商、模型和 key，点"测试连接"确认可用，之后日报会加上 AI 重排、翻译和摘要。
4. （可选）配置 Telegram、邮件或 Webhook 推送和推送时间，点"发送测试"确认能收到。
5. 每天到点后在首页看当期日报，也可以按日历翻看往期，把条目收藏或移到回收站。

### 命令行

在 Docker 里运行时，在命令前加 `docker compose exec web`。

```bash
newsd worker run-once            # 立即跑一轮：采集 + 给所有用户出日报
newsd worker runs                # 查看最近的运行记录
newsd issue build alice          # 只给用户 alice 生成今天的日报（--all 为所有用户，--no-ai 不调用模型）
newsd issue show alice           # 以 JSON 打印 alice 的日报
newsd push test alice telegram   # 用 alice 保存的配置发一条测试推送（email / telegram / webhook）
newsd push list                  # 查看最近的投递记录
```

### API

用 API key 调用接口，适合脚本或其他程序读取日报。先用账号登录拿到 cookie，再生成一个 key（只显示一次，服务器只存哈希）：

```bash
curl -c cookie.txt -d username=alice -d password='你的密码' http://localhost:8080/login
curl -b cookie.txt -X POST -d kind=api -d name=script http://localhost:8080/api/tokens
```

之后把 key 放在 `Authorization: Bearer <key>` 或 `X-News-API-Key` 请求头里：

```bash
curl -H "Authorization: Bearer $NEWSD_API_KEY" http://localhost:8080/api/me/issues
```

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # 测试全部使用模拟 HTTP，不需要联网
.venv/bin/ruff check .    # 代码检查
```

每次 push 和 Pull Request 都会在 GitHub Actions 上运行 ruff、pytest（Python 3.11 / 3.12）和 gitleaks 密钥扫描。

## 许可

[MIT](LICENSE)。依赖和参考项目的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
