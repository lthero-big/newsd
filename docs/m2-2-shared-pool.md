# M2-2 共享采集池 + trafilatura 全文与元数据抽取（DEPTAI-218）

## 改了什么

- **共享 HTTP 客户端** `newsd/pool/fetch.py`：asyncio + httpx，一个周期共用一个 `AsyncClient`。按域名限并发（默认每域名 2 个）和间隔（默认 1 秒），超时 12 秒，超时/连接错误/429/5xx 指数退避重试（尊重 Retry-After），响应体上限 3 MB。重定向手动跟随，每一跳都做 SSRF 检查（非 http、URL 带账号密码、localhost/.local/.internal、字面或解析出的内网/回环/链路本地/保留地址一律拒绝）。同一 URL 并发请求合并成一次。不读环境代理和 netrc。
- **共享条目池**（迁移 `0003_m2_pool`）：
  - `source_fetches`：(source_id, cycle) 唯一。谁先插入谁去抓，其余用户/进程直接用池子 —— 多个用户订阅同一个源，一个周期只发一次请求。
  - `pool_items`：按规范化 URL（去 utm/跟踪参数、www、末尾斜杠，存 sha256 作唯一键）去重；摘要够长（≥200 字）时再按 标题+摘要 的内容哈希去重（转载合并）。记录 first_seen/last_seen。
  - `pool_item_sources`：每个条目由哪些源带来、哪个周期、什么时候抓到。
  - 超过 7 天的旧条目不入池（像 HF blog 这种全量 feed 一次给 800+ 条）。
- **全文抽取** `newsd/pool/extract.py`：trafilatura 改为基础依赖（Apache-2.0）。
  - 按源开关：`sources.content_extractor`，参数 `sources.extractor_params` 里的 `favor_precision` / `favor_recall`。
  - HTML 由我们自己的 httpx 客户端取，不用 trafilatura 的下载。
  - 正文 + 元数据（标题、作者、发布日期、站点名、配图）一次拿全；拿不到日期退回原来的正则（`linkdates.from_html_regex`），拿不到或配图是站点通用卡片/徽章时退回原来的 og:image 正则。
  - `linkdates.from_html` 和 `media.og_media` 也改成先 trafilatura、后正则兜底。
  - 缓存表 `extractions`：按规范化 URL 缓存，另存 HTML 哈希，换了 URL 但页面相同也命中；所有用户共享。失败结果缓存 24 小时后再试。
  - 抽取失败（JS 渲染页、付费墙、反爬、HTTP 错误、非 HTML）只记状态，条目退回源里自带的摘要，整轮不失败。
  - 每源每周期最多抽 30 篇，只抽回看窗口（默认 26 小时）内的新条目。
  - 全文只在内部用（`item_text()` 给打分/摘要/翻译/语义去重/检索）；条目的 `summary` 字段仍是源的摘要，页面只展示摘要和原文链接。
- **去掉 X 连接器那条路**：删除 `x_from_connector.py` 和 `source_config.x_queries`（给连接器生成 search_posts_all 查询），X 只走自建 RSSHub（`/twitter/user`、`/twitter/keyword`，需 `RSSHUB_BASE` + `RSSHUB_ACCESS_KEY`，未配置时跳过）。
- **去掉本机 gh CLI 依赖**：GitHub 搜索插件只用公开 REST API；池子里设置 `NEWSD_GITHUB_TOKEN` 时会对 api.github.com 带 Bearer token。
- 旧的 hn/github/hf/ossinsight 插件在线程里跑，但它们发的每个请求都走共享 async 客户端（限速、SSRF、计数都生效）。
- 命令：`newsd pool run [--cycle 2026-10-20T09:00] [--kinds rss,reddit] [--no-extract] [--lookback-hours 26]`。

## 测试

`.venv/bin/pytest`：102 个通过（原 70 个里 8 个 X 连接器相关的随代码删除，新增 40 个）。

- `tests/pool/test_fetch.py`：SSRF（字面内网 IP、IPv6 回环、元数据地址、localhost、file://、URL 带密码、解析到内网、重定向到内网）、重定向上限、503 重试、超时重试、404 不重试、体积上限、同 URL 合并、按域名限速。
- `tests/pool/test_extract.py`：成功（正文+元数据一次拿全）、precision/recall 开关、JS 空壳失败、空/垃圾输入不抛错、正则兜底日期和配图、通用卡片过滤。
- `tests/pool/test_pool.py`：**5 个用户订阅同一个源，一个周期只请求一次**，同周期重跑 0 请求，下个周期再请求一次且不重复入池；URL 规范化 + 内容哈希去重；抽取缓存命中（同 URL、同 HTML 不同 URL）；失败兜底（JS 页、402 付费墙、403 反爬）且整轮继续；按源开关；坏源/内网源不影响整轮；X 无 RSSHub 时跳过、有时走 RSSHub 且不泄露 key；旧条目不入池、抽取数量上限。

## 真实 RSS 源对比（2026-10-20，每源最新 6 条，同一份 HTML 上比较）

脚本：`.venv/bin/python scripts/compare_extraction.py`。前 = 原来的正则日期 / 正则 og:image / 默认参数 trafilatura；后 = 新的 `extract_page`。

| 源 | 条目 | 发布日期 前→后 | 配图 前→后 | 全文≥200字 前→后 | 摘要可用 前→后 |
|---|---|---|---|---|---|
| hf-blog | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| openai-news | 6 | 0→0 | 0→0 | 0→0 | 0→0 |
| google-ai-blog | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| ollama-releases | 6 | 0→6 | 0→0 | 6→6 | 6→6 |
| vllm-releases | 6 | 0→6 | 0→0 | 6→6 | 6→6 |
| simonwillison | 6 | 0→6 | 0→0 | 6→6 | 6→6 |
| mit-tech-review-ai | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| techcrunch-ai | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| theverge-ai | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| arstechnica | 6 | 6→6 | 6→6 | 6→6 | 6→6 |
| marktechpost | 6 | 0→0 | 0→0 | 0→0 | 6→6 |
| infoq-cn | 6 | 0→6 | 6→6 | 6→6 | 6→6 |
| **合计** | 72 | 36/72 (50%) → 60/72 (83%) | 42/72 (58%) → 42/72 (58%) | 60/72 (83%) → 60/72 (83%) | 66/72 (92%) → 66/72 (92%) |

- 发布日期命中率 50% → 83%：GitHub releases、Simon Willison、InfoQ 中文页面原来的正则都拿不到，trafilatura 能拿到。
- 配图命中率持平（58%）：trafilatura 在 GitHub releases 上找到的是 opengraph.githubassets.com 通用卡片，按原规则过滤掉；其余站点两边一致。
- 正文和摘要命中率持平：原来也已经用 trafilatura 抽正文，这次主要是参数显式化、按源开关、缓存和失败兜底。
- openai.com 和 marktechpost 对我们的请求返回 403（反爬），两边都拿不到页面，走源摘要兜底（marktechpost 的 feed 摘要够长，所以“摘要可用”仍是 6/6）。

## 已知限制

- SSRF 检查在发请求前解析 DNS，httpx 连接时会再解析一次，理论上存在 DNS rebinding 窗口；要彻底堵住需要按解析出的 IP 直连并单独处理 TLS SNI，留到后续。
- 调度器（定时跑 `run_cycle`）、按用户打分/配额/翻译属于后面的 M2 子任务，这里只提供 `run_cycle`、`user_items`、`item_text` 接口。
