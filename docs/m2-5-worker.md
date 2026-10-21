# M2-5 调度器 + 独立 worker（DEPTAI-221）

## 做了什么

- **独立 worker 进程** `newsd worker start`（`newsd/worker.py`）：APScheduler `BlockingScheduler`，按 `NEWSD_WORKER_TIMES` 每天定点触发一次“管线”：
  1. **collect**：跑一次共享采集（M2-2 `run_cycle`，每源每周期只抓一次，含全文抽取）；
  2. **issues**：对每个启用的用户跑 `build_issue`（M2-3 规则打分/去重/配额 + M2-4 模型层，用户没填 key 就只跑规则），存成当期 `issues`。
  - 期号 = `NEWSD_WORKER_TZ` 时区下当天日期（`Asia/Shanghai` 07:00 触发的就是当天那期）；采集周期 = 触发时刻所在的 UTC 小时。
  - 幂等：同一小时再跑，池子直接复用（0 请求）；同一期号再跑，替换该用户当期结果，不会重复。
  - 容错：单个源失败只记错误；采集整体异常时仍用现有池子给所有用户出期；单个用户失败不影响其他人。任务 `max_instances=1`、`coalesce`，错过触发点 1 小时内补跑。
  - worker 启动时把上次异常退出遗留的 `running` 记录标成 `interrupted`。SIGTERM（`docker stop`）与 Ctrl-C 同样干净退出。
- **运行记录**（迁移 `0006_m2_worker`，表 `worker_runs`）：触发方式、采集周期、期号、状态（`ok` / `partial` / `failed` / `interrupted`）、开始/结束时间，`stages`（每阶段状态、起止、计数；采集阶段含 新条目/已见/抽取成功失败/请求数/失败源数，出期阶段含 成功/失败用户数和总条数），`users`（每个用户：状态、AI 状态、候选数、入选数、丢弃原因统计、模型请求数、耗时、错误），`errors`（前 50 条简短错误）。每个阶段、每个用户处理完立刻落库，运行中也能看到进度。
- **管理后台**（仅管理员）：`/admin` 页面新增“Worker 运行”一节（最近 20 次，可展开各阶段和各用户明细，内容均转义），接口 `GET /api/admin/worker-runs?limit=`、`GET /api/admin/worker-runs/{id}`（M3 已有的 `/api/admin/runs` 是按源抓取 / 按用户出期的视图，保持不变）。非管理员 403，未登录 401/跳转登录。
- **docker compose**：`web` 和 `worker` 共用同一镜像（`newsd:local`）、同一个 `/data` 卷（SQLite）。`worker` 等 `web` 健康（它先跑数据库迁移）后再启动；不依赖 GitHub Actions 或 Cloudflare Pages。SQLite 连接超时调到 30 秒，避免 web 与 worker 同时写时报 locked。镜像里补了 `config/`，所以 `docker compose exec web newsd db import-config --sources /app/config/collect-sources.json --tags /app/config/tags.json` 可以直接导入默认源目录。`tzdata` 加进了依赖（slim 镜像没有时区库）。

## 怎么跑

```bash
cp .env.example .env     # 填 NEWSD_SECRET_KEY，按需设 NEWSD_WORKER_TIMES / NEWSD_WORKER_TZ
docker compose up -d --build
docker compose exec web newsd admin create admin
docker compose exec web newsd db import-config --sources /app/config/collect-sources.json --tags /app/config/tags.json
docker compose logs -f worker
```

不用 docker：`newsd worker start`（常驻）；立刻手动跑一次：

```bash
newsd worker run-once [--no-ai] [--no-pool] [--no-extract] [--user alice] [--key 2026-10-21] [--cycle ...] [--since-hours 26]
newsd worker runs [--limit 10]          # 最近的运行记录
```

`run-once` 同样写运行记录（trigger=`manual`），任一阶段全失败时退出码为 1。

### 配置（环境变量 / .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWSD_WORKER_TIMES` | `07:00` | 每天触发时刻，`HH:MM`，逗号分隔多个 |
| `NEWSD_WORKER_TZ` | `UTC` | 上面时刻所用的时区（IANA 名，如 `Asia/Shanghai`），同时决定期号日期 |
| `NEWSD_WORKER_RUN_ON_START` | `0` | `1` = worker 启动后立刻额外跑一次 |
| `NEWSD_WORKER_EXTRACT` | `1` | `0` = 不抽取全文 |
| `NEWSD_WORKER_SINCE_HOURS` | `26` | 每期的候选窗口 |

时间、时区写错时 worker 启动即报错退出，不会静默。

## 测试

`.venv/bin/pytest`：166 个通过（含 M3 合入后的全部测试；`tests/worker/` 新增 14 个）。网络和模型全部是模拟的（`httpx.MockTransport`）。

- **一次 tick 端到端**：两个订阅同一个源的用户（alice 有模型 key、bob 没有），模拟 RSS + 模拟 OpenAI 端点；断言只请求一次源、两人都出了当期结果、alice 走模型（打分/翻译入库）而 bob 零模型请求、运行记录的阶段计数与落库内容（用新 session 读）一致。
- **真 APScheduler 触发真 `tick()`**：后台调度线程到点触发，走完整个管线并写运行记录。
- 同期重跑幂等（0 请求、期不重复）；源 500 → `partial` 但出期；单用户抛错不影响其他用户；采集抛错仍用旧池出期；全部失败 → `failed`；`--no-pool` / `--user` / 停用用户；启动时关闭遗留 `running`；时间/时区解析与时区日期期号（UTC 23:30 = 上海次日）；调度器按时间注册 cron 任务并算出正确的下次触发时间；CLI `run-once` / `runs`；管理后台接口与页面（含转义、非管理员 403）。

## 在真实 docker compose 上验证过的（2026-10-21）

本机有 docker：构建镜像，`docker compose up -d`（端口临时改到 18085、独立 project 名，验证完已 `down -v`）：

- `web` 健康后 `worker` 才启动；worker 日志显示 `scheduled daily pipeline 01:48 UTC`；
- 导入默认源目录、管理员订阅默认模板（24 个源）后，到 01:48 **定时触发**，真实访问网络采集：24 个源、59 个请求、193 条新条目（2 个 Reddit 源 429，记为失败源，运行状态 `partial`），随后为该用户生成当期结果 22 条（没配模型 key，`ai=skipped: no model key`）；
- 管理员用 cookie 访问（验证时管理页还是 M3 合并前的独立 `/admin/runs` 页，合并后改为 `/admin` 里的一节，由测试覆盖） `/api/admin/worker-runs`、`/api/admin/worker-runs/1`、`/admin` 页面都能看到这次运行的阶段与错误。

这次验证还抓到一个 bug 并补了测试：阶段字典是原地修改的，JSON 列看不到变化，阶段状态一直停在 `running`；改成每次保存都深拷贝。

**没验证的**：真实模型服务商（只用模拟端点）；Postgres 下的 worker；多个 worker 进程同时跑（设计上 pool 与 issue 都是幂等的，但没测）；跨天、夏令时边界。

## 已知限制 / 后续

- 触发时间是全站的（环境变量），用户各自的出刊时间/时区属于 M3 设置页；改时间需重启 worker。
- 采集一天只在触发点做（周期按小时），没有额外的“每小时补采”任务；需要的话可以把 `NEWSD_WORKER_TIMES` 写成多个时刻。
- Reddit 等源有时 429，目前只是记录，没有按源退避重试；运行记录里能看到是哪个源。
- 运行记录没有自动清理，量很小（每次一行 JSON）；需要时可按时间删。
- 迁移 `0007_m2_worker` 接在 M3 的 `0006_m3_marks` 之后（DEPTAI-222 先合入，已按此顺延）。
