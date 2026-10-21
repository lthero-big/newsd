# 部署到 Vercel + Supabase（实验性）

Vercel 运行 newsd 的 Web（整个 FastAPI 应用是一个 Serverless 函数），Supabase 提供 Postgres。没有常驻的 worker 进程，定时任务由外部定时器调用 `/api/cron/tick` 完成。

> **状态**：实验性。下面的流程已经在本地完整验证过（Postgres 16 + PgBouncer 事务模式模拟 Supabase 连接池，`VERCEL=1` / `VERCEL_ENV=production` 下运行构建步骤和 `newsd.vercel:app`，真实采集一个 RSS 源并生成日报；整套测试在这个连接池后面全部通过），但**还没有在真实的 Vercel / Supabase 账号上跑过**。需要稳定运行请优先用 Docker。

## 和 Docker 部署的区别

| | Docker / 本机 | Vercel + Supabase |
| --- | --- | --- |
| 定时任务 | worker 进程按 `NEWSD_WORKER_TIMES` 调度，每分钟检查推送 | 外部调用 `/api/cron/tick`：Hobby 版 Vercel Cron 每天 1 次；更频繁用 GitHub Actions 或 Pro |
| 单次运行时长 | 不限 | 函数上限：Hobby 300 秒，Pro 800 秒 |
| 数据库 | SQLite 或 Postgres | 只能 Postgres（函数的文件系统只读且不共享） |
| 升级 | 网页一键更新 / `docker compose pull` | 推送到仓库或 Redeploy，构建时迁移 |

## 文件

- `newsd/vercel.py`：入口，`pyproject.toml` 里 `[tool.vercel] entrypoint = "newsd.vercel:app"`。设置 Vercel 下的默认值（`NEWSD_DEPLOY_MODE=vercel`、不在客户端保留连接池、`NEWSD_COOKIE_SECURE=1`）。缺 `NEWSD_SECRET_KEY` 或数据库还是 SQLite 时，所有请求都返回 503 并说明缺什么，而不是一个看不出原因的 500。
- `newsd/vercel_build.py`：构建步骤（`[tool.vercel.scripts] build`），在新部署上线前迁移数据库。
- `vercel.json`：函数最长运行 300 秒、排除测试和文档、每天一次的 Cron。
- `newsd/cron.py`：`/api/cron/tick`。
- `examples/github-actions/newsd-tick.yml`：GitHub Actions 定时器模板。

## 步骤

### 1. Supabase

1. 新建项目，记下数据库密码。
2. 点项目顶部的 **Connect**，复制 **Transaction pooler** 连接串：

   ```
   postgresql://postgres.<项目ref>:<密码>@aws-<n>-<region>.pooler.supabase.com:6543/postgres
   ```

   主机名要从控制台复制，不能按区域自己拼。密码里有 `@ # ? &` 或空格时要做 URL 编码。

### 2. Vercel

1. Fork 本仓库，在 Vercel **Add New → Project** 导入 fork。框架预设会识别为 FastAPI，不需要改构建命令和输出目录。
2. **Settings → Environment Variables**，作用范围选 **Production**：

   | 变量 | 必填 | 说明 |
   | --- | --- | --- |
   | `NEWSD_SECRET_KEY` | 是 | 主密钥，生成后不要更换，和数据库一起备份 |
   | `NEWSD_DATABASE_URL` | 是 | Supabase Transaction pooler 连接串。`postgresql://` 开头即可，newsd 会自动使用 psycopg 驱动 |
   | `CRON_SECRET` | 是 | 至少 16 位随机串。Vercel Cron 会自动以 `Authorization: Bearer <CRON_SECRET>` 调用 |
   | `NEWSD_WORKER_TIMES` | 否 | 出日报的时刻，默认 `07:00` |
   | `NEWSD_WORKER_TZ` | 否 | 上面时刻所在的时区，默认 `UTC` |
   | `NEWSD_PUBLIC_URL` | 否 | 例如 `https://newsd.example.com`，推送消息里的链接用它 |
   | `NEWSD_MIGRATE_DATABASE_URL` | 否 | 只给构建时迁移用的连接串，建议填 Supabase **Session pooler**（端口 5432） |

3. 部署。构建日志里会看到 `newsd: database schema (empty) -> 0012_...`。迁移失败时构建失败，线上仍是上一个版本。
4. 打开 `https://<项目>.vercel.app/healthz`，应返回 `{"ok": true, ...}`。

### 3. 创建管理员

在任意装了 newsd 的机器上（`pip install -e .`），用同一个连接串和主密钥执行：

```bash
NEWSD_DATABASE_URL='postgresql://postgres.<ref>:<密码>@<pooler-host>:6543/postgres' \
NEWSD_SECRET_KEY='<同一个主密钥>' \
newsd create-admin admin
```

然后登录，在管理后台导入信息源（或本机执行 `newsd db import-config --sources config/collect-sources.json --tags config/tags.json`，带上同样的两个环境变量）。

### 4. 定时任务

`vercel.json` 默认：

```json
"crons": [{ "path": "/api/cron/tick", "schedule": "0 7 * * *" }]
```

Vercel Cron 的时间是 **UTC**。把它改成 `NEWSD_WORKER_TIMES` 换算到 UTC 的那个小时：例如 `NEWSD_WORKER_TIMES=07:00`、`NEWSD_WORKER_TZ=Asia/Shanghai` 对应 UTC 23:00，写 `0 23 * * *`。

**Hobby（免费版）的限制**：

- 每个 Cron 每天最多触发一次，写成更频繁的表达式会部署失败；
- 在设定的那个小时内随机触发（`0 23 * * *` 可能在 23:00–23:59 之间），所以日报会晚最多一小时；
- 只有这一次调用时，用户在设置里选的推送时间不生效：推送在每天这次运行之后统一发出；
- 一天多期（`NEWSD_WORKER_TIMES=07:00,19:00`）需要每个时刻都有一次调用。

需要更频繁时，二选一：

- **GitHub Actions（免费）**：把 `examples/github-actions/newsd-tick.yml` 复制到 fork 的 `.github/workflows/`，在 fork 的 **Settings → Secrets and variables → Actions** 里添加变量 `NEWSD_URL`（`https://<项目>.vercel.app`）和 Secret `NEWSD_CRON_SECRET`（和 Vercel 的 `CRON_SECRET` 相同）。它每 10 分钟调用一次。GitHub 的定时任务可能延迟几分钟，仓库 60 天没有提交时会被自动停用（在 Actions 页面重新启用即可）。
- **Vercel Pro**：把 schedule 改成 `*/10 * * * *`，并可把函数时长提高到 800 秒。

两个都开也没问题，也可以用 cron-job.org 之类的外部服务，只要带上 `Authorization: Bearer <CRON_SECRET>` 请求头。

## `/api/cron/tick` 做什么

每次调用：

1. 把被平台中止、超过 35 分钟（`NEWSD_CRON_STALE_MINUTES`）仍显示「运行中」的定时运行标记为「中断」；
2. 找到最近一个已经过去的日报时刻；如果这个时刻之后还没有定时运行，就跑一次完整日报（采集共享池 → 每个用户生成日报）；
3. 发送到期的推送（每个用户自己的推送时间、失败重试）；
4. 最多每 6 小时检查一次新版本。

- **幂等**：同一个时刻重复调用只跑一次。Vercel Cron 可能漏发或重复发，都没关系。
- **不会并发**：每次调用先写一条运行记录再检查有没有更早的，两个同时到达的调用只有一个会跑日报（本地验证：6 个并发调用，1 个运行、5 个返回 `busy`）。
- 参数：`?run=auto`（默认）、`?run=force`（不管这个时刻有没有跑过都跑一次）、`?run=skip`（只发推送）。GET 和 POST 都可以。
- 鉴权：`Authorization: Bearer <密钥>` 或 `X-Newsd-Cron-Key: <密钥>`。密钥取 `NEWSD_CRON_SECRET`，没有就取 `CRON_SECRET`；都没设时接口返回 503。不接受放在 URL 里的密钥（会进访问日志）。
- 每次运行都会出现在「管理 → 日志与任务」里，触发方式显示为「定时接口」。

返回示例：

```json
{
  "ok": true, "slot": "2026-10-21T07:00:00Z", "mode": "auto", "interrupted": [],
  "pipeline": {"ran": true, "run": {"id": 8, "status": "ok", "issue_key": "2026-10-21", "seconds": 25.8, "users_ok": 1, "users_failed": 0}},
  "push": {"planned": 0, "attempted": 0, "ok": 0, "retrying": 0, "failed": 0, "skipped": 0}
}
```

## 连接池说明

- Serverless 函数会开很多短连接，所以运行时要用 Supabase 的 **Transaction pooler（6543）**。
- 事务模式不支持预编译语句。newsd 在端口 6543、或运行在 Vercel 上时，会自动关闭 psycopg 的自动预编译（`prepare_threshold=None`）；其他事务模式连接池（例如自建 PgBouncer 用别的端口）请设置 `NEWSD_DB_PREPARE=0`。
- Vercel 上不在客户端保留连接池（SQLAlchemy `NullPool`），避免函数冻结后留下失效连接。
- 连接 `*.supabase.com` / `*.supabase.co` 且连接串里没写 `sslmode` 时，newsd 默认 `sslmode=require`。
- 迁移和备份是长会话，用 **Session pooler（5432）** 或直连地址更合适：迁移可以设 `NEWSD_MIGRATE_DATABASE_URL`，备份用 `pg_dump`。免费版 Supabase 的直连地址只有 IPv6。

## 迁移和预览部署

- **生产构建**（`VERCEL_ENV=production`）会迁移数据库。迁移失败构建就失败，线上不受影响。
- **预览部署**默认不迁移，避免一个还没合并的分支改掉生产库的表结构。建议预览环境不要配置 `NEWSD_DATABASE_URL`，或者配一个单独的 Supabase 项目/分支库，再设 `NEWSD_MIGRATE_ON_BUILD=1`。
- 兜底：生产部署冷启动时如果发现表结构落后，也会迁移一次。`NEWSD_MIGRATE_ON_START=0` / `NEWSD_MIGRATE_ON_BUILD=0` 可以关闭。

## 备份

- 数据库：Supabase 免费版有每日自动备份；自己备份用 `pg_dump 'postgresql://postgres.<ref>:<密码>@<pooler-host>:5432/postgres' > newsd.sql`（Session pooler）。
- `NEWSD_SECRET_KEY`：存到密码管理器。

## 在本地验证

不需要 Vercel 或 Supabase 账号：

```bash
# Postgres + 事务模式的 PgBouncer（模拟 Supabase 的 6543）
docker network create nd
docker run -d --name nd-pg --network nd -e POSTGRES_USER=newsd -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=newsd postgres:16-alpine
docker run -d --name nd-pgb --network nd -p 127.0.0.1:6543:6543 -e DB_HOST=nd-pg -e DB_USER=newsd -e DB_PASSWORD=pw -e DB_NAME=newsd \
  -e POOL_MODE=transaction -e MAX_PREPARED_STATEMENTS=0 -e LISTEN_PORT=6543 -e AUTH_TYPE=scram-sha-256 edoburu/pgbouncer

export VERCEL=1 VERCEL_ENV=production NEWSD_SECRET_KEY=local-secret CRON_SECRET=local-cron-secret-123456 \
       NEWSD_DATABASE_URL=postgresql://newsd:pw@127.0.0.1:6543/newsd
python -m newsd.vercel_build                       # 构建步骤：迁移
newsd create-admin admin
uvicorn newsd.vercel:app --port 8080               # 和 Vercel 上同一个入口
curl -H "Authorization: Bearer $CRON_SECRET" http://127.0.0.1:8080/api/cron/tick
```

整套测试也可以跑在 Postgres（或这个连接池）后面：`NEWSD_TEST_PG_URL=postgresql://newsd:pw@127.0.0.1:6543/newsd NEWSD_DB_POOL=null pytest -q`（会清空这个库）。
