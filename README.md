# newsd

可本地部署的多用户 AI 日报服务，由 news-daily 改造而来（DEPTAI-216）。Python 一套到底：FastAPI + SQLAlchemy（默认 SQLite，可选 Postgres）+ APScheduler + Jinja2。

## 当前进度

- [x] M0 新仓库：全新 git 历史，只带代码，不带 data/、editions/、私有端点和私有 key。
- [x] M1（骨架）：账号（admin/user）、邀请码注册、session 登录、错误口令 3 次锁 IP 15 分钟、每用户 API key / MCP token（只存哈希）、用户密钥加密存储、管理员命令、Dockerfile 和 docker compose。
- [ ] M2 管线迁移：共享采集池、按用户打分/配额/翻译、调度器。采集代码已放在 `newsd/collector/`（从 news-daily 原样搬入，待接线）。
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

## 测试

```bash
.venv/bin/pytest
```
