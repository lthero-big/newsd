# newsd

可本地部署的多用户 AI 日报服务，由 news-daily 改造而来（DEPTAI-216）。Python 一套到底：FastAPI + SQLAlchemy（默认 SQLite，可选 Postgres）+ APScheduler + Jinja2。

## 当前进度

- [x] M0 新仓库：全新 git 历史，只带代码，不带 data/、editions/、私有端点和私有 key。
- [x] M1（骨架）：账号（admin/user）、注册（邀请码或公开注册，可随时切换）、session 登录、错误口令 3 次锁 IP 15 分钟、每用户 API key / MCP token（只存哈希）、用户密钥加密存储、管理员命令、Dockerfile 和 docker compose。
- [ ] M2 管线迁移：
  - [x] M2-1 源目录、订阅、关键词、配额进数据库。
  - [x] M2-2 共享采集池（每源每周期只抓一次，`newsd pool run`）+ trafilatura 全文与元数据抽取（按源开关、共享缓存、失败退回源摘要），X 只走自建 RSSHub，不再依赖本机 gh CLI。见 [docs/m2-2-shared-pool.md](docs/m2-2-shared-pool.md)。
  - [x] M2-4 模型层：多服务商 LLM 客户端（OpenAI 兼容 / Anthropic / Gemini / DeepSeek / 通义 / 豆包 / Ollama，限速、并发上限、退避重试、非法 JSON 修复一次）、用户 key 用服务器主密钥加密存储、设置页“测试连接”、全站共享翻译缓存、按类别的处理配置（Markdown 提示词 + 阈值）、可选 AI 重排和语义去重；没有 key 时管线照常跑（`newsd process run <用户>`）。见 [docs/m2-4-model-layer.md](docs/m2-4-model-layer.md)。
  - [x] M2-3 按用户打分、过滤与分组配额：关键词（关注/加权/屏蔽，有全文时匹配全文）规则打分，先按 URL 和标题相似度去重，再按话题/分组/模块/总量上限裁剪，每期结果存库（`issues`/`issue_items`），有 key 时叠加 M2-4 模型打分。`newsd issue build <用户>|--all`、`GET /api/me/issues/latest`。见 [docs/m2-3-scoring.md](docs/m2-3-scoring.md)。
  - [ ] 调度器。
- [ ] M3 前端：日报页动态渲染、设置页、管理后台、固定调色板。
- [ ] M4 推送：Telegram / 邮件 / Webhook。
- [ ] M5 迁移导入、中英文部署文档、LICENSE。

## 快速开始

```bash
cp .env.example .env            # 填 NEWSD_SECRET_KEY
uv venv && uv pip install -e '.[dev]'
.venv/bin/newsd admin create admin
.venv/bin/newsd serve           # http://localhost:8080
```

或者 `docker compose up -d --build`，然后 `docker compose exec web newsd admin create admin`。

## 注册方式

三种模式，管理员随时可以切换：

- `invite`（默认）：凭邀请码注册，每个码只能用一次。`newsd admin invite` 生成邀请码。
- `open`：公开注册，任何人都能在 `/register` 注册；填了邀请码也照样认。
- `closed`：关闭注册，只能由管理员在命令行建账号。

默认值写在 `.env` 的 `NEWSD_REGISTRATION`。运行中切换：`newsd admin registration open`（不带参数是查看当前模式），或管理员调用 `POST /api/admin/registration`（表单字段 `mode`）。切换结果存在数据库里，优先于 `.env`。

## 测试

```bash
.venv/bin/pytest
```
