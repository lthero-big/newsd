# newsd

[![CI](https://github.com/lthero-big/newsd/actions/workflows/ci.yml/badge.svg)](https://github.com/lthero-big/newsd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**可自托管的多用户 AI 日报服务。** 每个用户订阅自己的信息源和关键词，设定配额，每天定时拿到一份去重、打分、分组后的日报，可选用自己的大模型 key 做重排、翻译和摘要，并推送到 Telegram / 邮件 / 飞书 / 钉钉 / Slack / Discord / 通用 Webhook。

**newsd is a self-hosted, multi-user AI news digest.** Each user subscribes to their own sources and keywords, sets quotas, and gets a deduplicated, scored, grouped daily report on a schedule. An optional per-user LLM key adds re-ranking, translation and summaries, and reports can be pushed to Telegram, e-mail, Feishu, DingTalk, Slack, Discord or any webhook. One Python codebase: FastAPI + SQLAlchemy (SQLite by default, Postgres optional) + APScheduler + Jinja2. MIT licensed.

<!-- 截图 / screenshots: 放到 docs/images/ 后在这里引用 -->

## 功能

- **多用户**：admin / user 角色；邀请码、公开注册或关闭注册三种模式，管理员可随时切换；错误口令 3 次锁 IP 15 分钟；每用户 API key / MCP token 只存哈希。
- **共享采集池**：HN、Reddit、RSS/Atom、GitHub、Hugging Face、OSSInsight，以及经你自建 RSSHub 的 X 等源；每源每周期只抓一次，所有用户共享；trafilatura 抽取全文和发布日期；带 SSRF 防护。
- **按用户出期**：关键词（关注 / 加权 / 屏蔽）规则打分，URL 和标题相似度去重，按话题 / 分组 / 模块 / 总量配额裁剪，每期入库。
- **模型层（可选）**：OpenAI 兼容、Anthropic、Gemini、DeepSeek、通义、豆包、Ollama；用户 key 用服务器主密钥加密；没有 key 时规则管线照常运行；全站共享翻译缓存。
- **定时 worker**：APScheduler 按时区和时刻先采集再给每个用户出期，运行记录可在管理后台查看。
- **推送**：Telegram、SMTP 邮件、Webhook（通用 / 飞书 / 钉钉 / Slack / Discord，`#{变量}` 模板），失败退避重试、投递记录、一键测试。
- **Web 界面**：日报、日历、收藏、回收站、设置页、管理后台（用户、邀请码、带质量统计的源目录、运行记录、推送记录）。

## 当前进度

- [x] M0 新仓库：全新 git 历史，只带代码，不带 data/、editions/、私有端点和私有 key。
- [x] M1（骨架）：账号（admin/user）、注册（邀请码或公开注册，可随时切换）、session 登录、错误口令 3 次锁 IP 15 分钟、每用户 API key / MCP token（只存哈希）、用户密钥加密存储、管理员命令、Dockerfile 和 docker compose。
- [ ] M2 管线迁移：
  - [x] M2-1 源目录、订阅、关键词、配额进数据库。
  - [x] M2-2 共享采集池（每源每周期只抓一次，`newsd pool run`）+ trafilatura 全文与元数据抽取（按源开关、共享缓存、失败退回源摘要），X 只走自建 RSSHub，不再依赖本机 gh CLI。见 [docs/m2-2-shared-pool.md](docs/m2-2-shared-pool.md)。
  - [x] M2-4 模型层：多服务商 LLM 客户端（OpenAI 兼容 / Anthropic / Gemini / DeepSeek / 通义 / 豆包 / Ollama，限速、并发上限、退避重试、非法 JSON 修复一次）、用户 key 用服务器主密钥加密存储、设置页“测试连接”、全站共享翻译缓存、按类别的处理配置（Markdown 提示词 + 阈值）、可选 AI 重排和语义去重；没有 key 时管线照常跑（`newsd process run <用户>`）。见 [docs/m2-4-model-layer.md](docs/m2-4-model-layer.md)。
  - [x] M2-3 按用户打分、过滤与分组配额：关键词（关注/加权/屏蔽，有全文时匹配全文）规则打分，先按 URL 和标题相似度去重，再按话题/分组/模块/总量上限裁剪，每期结果存库（`issues`/`issue_items`），有 key 时叠加 M2-4 模型打分。`newsd issue build <用户>|--all`、`GET /api/me/issues/latest`。见 [docs/m2-3-scoring.md](docs/m2-3-scoring.md)。
  - [x] M2-5 调度器 + 独立 worker：APScheduler 按配置的时刻（时区可配）先跑一次共享采集，再给每个用户出当期结果；运行记录（起止、各阶段计数、每用户结果、错误）入库，管理员在 `/admin` 的“Worker 运行”查看；`docker compose up` 同时起 web 和 worker，不依赖 GitHub Actions / Cloudflare Pages。`newsd worker start|run-once|runs`。见 [docs/m2-5-worker.md](docs/m2-5-worker.md)。
- [x] M3 前端：按用户动态渲染的日报 / 日历 / 收藏 / 回收站、设置页（订阅源、关键词、配额、推送偏好、模型与 key、类别阈值）、管理后台（用户、邀请码、注册方式、带质量统计的源目录、运行记录）、固定调色板换色（不再每周自动轮换）、注册后一键订阅预设源。见 [docs/m3-222-pages.md](docs/m3-222-pages.md)。
- [x] M4 推送（DEPTAI-223）：Telegram / 邮件（SMTP）/ Webhook（通用、飞书、钉钉、Slack、Discord，`#{变量}` 模板，可选“一条汇总”或“汇总 + 每条一消息”），按每个用户设置的时间在日报生成后发送；失败退避重试，投递记录入库，管理后台“推送记录”可查；设置页每个渠道有“发送测试”，密钥加密存储、不进日志。`newsd push run|test|list`。见 [docs/m4-223-push.md](docs/m4-223-push.md)。
- [x] M5 公开发布准备（DEPTAI-224）：MIT LICENSE、第三方许可清单、README / 中英文部署文档、GitHub Actions CI（ruff + pytest + gitleaks）、工作树和全部 git 历史的隐私与密钥扫描、公开前检查清单。见 [docs/m5-224-release.md](docs/m5-224-release.md) 和 [docs/public-release-checklist.md](docs/public-release-checklist.md)。仓库仍是私有，公开需要负责人确认。

## 快速开始

**Docker（推荐）**

```bash
git clone https://github.com/lthero-big/newsd.git && cd newsd
cp .env.example .env            # 至少把 NEWSD_SECRET_KEY 改成一串长随机字符：python3 -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up -d --build    # 同时启动 web（:8080）和定时 worker
docker compose exec web newsd admin create admin
docker compose exec web newsd db import-config --sources /app/config/collect-sources.json --tags /app/config/tags.json
```

打开 http://localhost:8080 登录。**NEWSD_SECRET_KEY 一旦设定就别再改**：用户存的模型 key / 推送密钥都用它加密，换了就解不开。

**不用 Docker**

```bash
cp .env.example .env
uv venv && uv pip install -e '.[dev]'     # 或 python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/newsd admin create admin
.venv/bin/newsd db import-config
.venv/bin/newsd serve                      # http://localhost:8080
.venv/bin/newsd worker start               # 另开一个终端：定时采集和出期
```

部署细节（反向代理 HTTPS、Postgres、systemd、备份、升级）见 [docs/deployment.md](docs/deployment.md) / [docs/deployment.en.md](docs/deployment.en.md)。

## 配置

全部通过环境变量（`.env`）。[.env.example](.env.example) 是带注释的完整示例，不含任何真实值。`config/collect-sources.json` 和 `config/tags.json` 是默认的公开源目录与标签词表（只有公开网址，没有密钥），用 `newsd db import-config` 导入数据库后，再在管理后台和设置页里增删。需要密钥的源（自建 RSSHub、GitHub token）只读环境变量，不写进配置文件。

## 注册方式

三种模式，管理员随时可以切换：

- `invite`（默认）：凭邀请码注册，每个码只能用一次。`newsd admin invite` 生成邀请码。
- `open`：公开注册，任何人都能在 `/register` 注册；填了邀请码也照样认。
- `closed`：关闭注册，只能由管理员在命令行建账号。

默认值写在 `.env` 的 `NEWSD_REGISTRATION`。运行中切换：`newsd admin registration open`（不带参数是查看当前模式），或管理员调用 `POST /api/admin/registration`（表单字段 `mode`）。切换结果存在数据库里，优先于 `.env`。

## 开发与测试

```bash
.venv/bin/pytest          # 测试（全部用模拟 HTTP，不联网）
.venv/bin/ruff check .    # 代码检查，CI 跑的是同一条命令
```

CI（GitHub Actions）在每次 push / PR 上跑 ruff、Python 3.11 和 3.12 的 pytest，以及 gitleaks 密钥扫描，见 [docs/m5-224-release.md](docs/m5-224-release.md)。

## 许可

[MIT](LICENSE)。依赖和参考项目的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。


