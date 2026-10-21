# M4 推送：Telegram / 邮件 / Webhook（DEPTAI-223）

## 做了什么

每个用户在设置页里配置自己的推送时间和渠道；worker 在日报生成之后、到了该用户设定的时间就推送，失败自动退避重试，每次投递落库，管理员在 `/admin` 的“推送记录”查看。代码在 `newsd/push/`：

| 文件 | 内容 |
|---|---|
| `prefs.py` | 每用户渠道配置的校验与存储（密钥加密，见下） |
| `digest.py` | 把一期日报变成消息：一条汇总，或“汇总 + 每条一消息”；按渠道转义（Markdown / Slack mrkdwn / Telegram HTML / 纯文本），按平台长度限制裁剪 |
| `template.py` | Webhook 模板（`#{变量}` 占位符，仿 Horizon 的 `request_body`） |
| `channels.py` | Telegram、SMTP、Webhook（通用 / 飞书 / 钉钉 / Slack / Discord）发送；错误分类（可重试 / 不可重试）、脱敏、SSRF 防护 |
| `service.py` | 调度（谁该发了）、重试与退避、投递记录、测试推送 |
| `api.py` | `/api/me/push`、`/api/me/push/test/{channel}`、`/api/me/push/deliveries`、`/api/admin/push-deliveries` |

迁移 `0008_m4_push`（表 `push_deliveries`）。`newsd push run|test|list` 三个命令。

### 什么时候发

worker 每分钟跑一次 `push-dispatch`，每次 pipeline 跑完后也立即跑一次。对每个开启了推送的活跃用户：

1. 用户时区的当前时间已过他设定的时间，且没超过宽限（`NEWSD_PUSH_GRACE_HOURS`，默认 12 小时；防止晚上才开启推送的人收到一份过时的早报）；
2. 当期日报已存在（期号 = `NEWSD_WORKER_TZ` 时区下的今天；没生成好就下一分钟再看）；
3. 该（用户, 期, 渠道）还没有定时投递记录 —— 每期每渠道只发一次。

满足就给每个启用的渠道各建一条投递记录并发送。**建议把 `NEWSD_WORKER_TZ` 设成大多数用户所在的时区**（期号和“今天”按它算）。

### 重试

- 可重试：网络错误/超时、HTTP 5xx、HTTP 429（遵守 `Retry-After`/`retry_after`）、SMTP 4xx、飞书限流（11232）、钉钉发送过快。按 `NEWSD_PUSH_BACKOFF`（默认 `60,300,900` 秒）退避，最多 `NEWSD_PUSH_MAX_ATTEMPTS`（默认 4）次，之后状态 `failed`。
- 不可重试（直接 `failed`）：HTTP 4xx、重定向（不跟随）、token/SMTP 登录被拒、收件人被拒、被 SSRF 规则拦下、飞书/钉钉/Slack 返回业务错误码。
- 状态：`pending` → `retrying`（带下次重试时间）→ `ok` / `failed`；配置被关掉或期被删除记为 `skipped`。
- “汇总 + 每条一消息”重试时从中断处继续（`progress` 记已发条数），不会重复发已送达的消息。
- 行在发送前用租约（`next_attempt_at` 往后推 10 分钟）认领，两个调度器同时跑也不会重复发；崩溃后租约到期自动接着发。
- 每次尝试记在 `log`（序号、时间、是否成功、发了几条、错误）。

### 密钥怎么存

用户设置 `user_settings(key='push')`：`value` 存非敏感内容（时间、时区、邮箱、Telegram chat id、Webhook 类型/方式/模板，以及各渠道开关）；**Telegram bot token、Webhook 地址（Slack/飞书等的地址本身就是凭据）、Webhook 签名密钥**合成一个 JSON，用服务器主密钥（`NEWSD_SECRET_KEY`，Fernet，同模型 key）加密放进 `secret` 列。接口和设置页只返回“后 4 位”提示；PUT 时字段省略 / `null` = 保持不变，`""` = 清除。发送错误信息入库前会去掉 token、已知密钥以及任何 URL 的路径部分，所以管理页和用户历史里看不到凭据；日志里也不输出。SMTP 密码只在服务器环境变量里。

## 怎么配置

### 服务器（`.env`，管理员）

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWSD_SMTP_HOST` / `NEWSD_SMTP_PORT` | — / `587` | 邮件渠道必需 |
| `NEWSD_SMTP_USER` / `NEWSD_SMTP_PASSWORD` | 空 | 留空则不登录 |
| `NEWSD_SMTP_FROM` | 空（回退到 SMTP_USER） | 发件人 |
| `NEWSD_SMTP_SECURITY` | `starttls` | `starttls` / `ssl` / `none` |
| `NEWSD_TELEGRAM_BOT_TOKEN` | 空 | 可选的默认机器人；用户没填自己的 token 时用它 |
| `NEWSD_TELEGRAM_API_BASE` | `https://api.telegram.org` | 自建 Bot API 时改 |
| `NEWSD_PUBLIC_URL` | 空 | 填了，消息里会带“查看完整日报”链接（`<url>/issue/<期>`） |
| `NEWSD_PUSH_MAX_ATTEMPTS` / `NEWSD_PUSH_BACKOFF` | `4` / `60,300,900` | 重试 |
| `NEWSD_PUSH_GRACE_HOURS` | `12` | 过了设定时间多久内还补发 |
| `NEWSD_PUSH_MAX_ITEMS` / `NEWSD_PUSH_ITEM_DELAY` | `20` / `0.5` | 逐条模式最多发几条、两条之间间隔秒数 |
| `NEWSD_PUSH_TIMEOUT` | `15` | 单次请求超时（秒） |
| `NEWSD_PUSH_ALLOW_PRIVATE` | `0` | `1` = 允许 Webhook 指向内网/本机地址 |

### 用户（设置页 → 推送）

通用：开关、时间（`HH:MM`）、时区（IANA 名）。每个渠道一个“发送测试”按钮（先保存表单，再用已保存的配置发一条示例；Webhook 的测试走真实模板和所选的“汇总/逐条”方式，所以能直接验证模板；每分钟最多 5 次）。

- **邮件**：填收件邮箱；SMTP 由管理员配置。每期一封（text + HTML 两种格式），不分“逐条”。
- **Telegram**：在 @BotFather 建机器人拿 token；chat id 是数字（先给机器人发一条消息，再看 `getUpdates`）或 `@频道名`（机器人需是频道管理员）。token 可留空，使用服务器默认机器人。消息用 HTML 格式。
- **Webhook**：地址 + 类型 + 方式，可选签名密钥和自定义模板。
  - 类型：`generic`（通用 JSON）、`feishu`（飞书，Card JSON 2.0）、`dingtalk`（钉钉 markdown）、`slack`（mrkdwn，链接为 `<url|标题>`）、`discord`（`content`，限 1900 字符，禁用 @提及）。
  - 方式：`summary` 一条汇总消息（超出平台长度时只列放得下的前 N 条，并注明“另 M 条”）；`summary_and_items` 先发一条概览（只列标题），再每条日报一条消息（最多 `NEWSD_PUSH_MAX_ITEMS` 条）。
  - 签名密钥：飞书“签名校验”（在请求体里加 `timestamp`/`sign`）、钉钉“加签”（在 URL 上加 `timestamp`/`sign`）。钉钉“自定义关键词”安全设置需要关键词出现在内容里（默认标题含 “newsd”，自己的模板里写上你设的关键词）。
  - 安全：Webhook 地址由用户填写，所以发送前会解析主机，拒绝回环、内网、链路本地（含云厂商元数据地址）和 `localhost`，且不跟随重定向。内网接收端请让管理员设 `NEWSD_PUSH_ALLOW_PRIVATE=1`。

### Webhook 模板占位符

写法沿用 Horizon 的 `#{name}`，可选截断 `#{name?limit=N&split=分隔符}`（超过 N 字符时在 N 以内最后一个分隔符处截断）。

| 占位符 | 含义 |
|---|---|
| `#{date}` / `#{key}` | 期号（日期） |
| `#{user}` | 用户名 |
| `#{message_title}` | 消息标题，如 `newsd 日报 2026-10-21` |
| `#{message_kind}` | `summary` / `overview` / `item` |
| `#{summary}` | 这条消息的正文（该渠道格式的 Markdown / mrkdwn；逐条消息里就是这一条的内容） |
| `#{item_count}` | 本期总条数 |
| `#{issue_url}` | 日报网页地址（需要 `NEWSD_PUBLIC_URL`，否则为空） |
| `#{timestamp}` | Unix 秒 |
| `#{item_index}` `#{item_title}` `#{item_url}` `#{item_summary}` `#{item_source}` `#{item_module}` `#{item_score}` | 仅逐条消息；其他消息里为空 |

两个模板框：**汇总模板**（汇总消息和概览）和**单条模板**（逐条消息；留空则同汇总模板）。模板留空用内置格式；填：

- **JSON 对象/数组** → 就是完整请求体，逐个字符串替换占位符（值里有引号、换行也不会破坏 JSON；飞书/钉钉的签名仍会自动加上）。例：`{"msgtype":"text","text":{"content":"#{message_title}\n#{summary}"}}`
- **普通文本** → 只替换消息正文，外层按所选平台的格式封装。例：`#{date} 共 #{item_count} 条\n#{summary}`

保存时会检查未知占位符。

## 怎么跑

```bash
# .env 里配好 NEWSD_SMTP_* / NEWSD_PUBLIC_URL 等；docker compose 的 worker 服务已包含推送
docker compose up -d --build
# 不用 docker：
newsd worker start           # 每分钟 push-dispatch + 每天定时 pipeline
newsd push test alice webhook   # 用 alice 保存的配置发一条测试（email|telegram|webhook）
newsd push run               # 立即发送已到期的和该重试的
newsd push list --limit 20   # 最近的投递记录
```

管理员在 `/admin` → “推送记录”看每条投递的用户、渠道、期、状态、尝试次数、已发/总条数、下次重试和（已脱敏的）错误；API：`GET /api/admin/push-deliveries?limit=&status=&channel=`。用户自己的历史：`GET /api/me/push/deliveries`。

## 测试

`python -m pytest`（或 `.venv/bin/pytest`；`pyproject.toml` 里加了 `pythonpath = ["."]`，在 worktree 里用共用 venv 也测的是当前目录的代码）：224 个通过（M2-5/M3 基线 166 + 新增 58，在 `tests/push/`）。没有任何真实网络：HTTP 用 `httpx.MockTransport`，邮件用测试里起的真 SMTP 服务（`127.0.0.1` 随机端口，支持 AUTH / 拒收 / 4xx / 5xx 应答），DNS 解析被替换。

- 模板与消息：占位符、`limit/split` 截断、JSON 模板逐字符串渲染、未知占位符被拒、汇总限长与“另 N 条”、逐条模式、各格式转义（标题里的 HTML、`javascript:` 链接）。
- 渠道：Telegram（请求体、401/429/5xx/业务错误、token 不出现在错误里）；邮件（multipart、AUTH 登录、登录被拒/收件人被拒/451/554/服务器不可达的分类）；Webhook 通用/飞书/钉钉/Slack/Discord 的请求体、飞书与钉钉签名（独立重算 HMAC 比对）、各平台业务错误码、自定义 JSON/文本模板、SSRF 拒绝（内网 IP、localhost、解析到内网的域名，且零请求发出）。
- 调度与重试：时间未到不发/到点只发一次、用户时区、日报没生成先等、宽限期、关闭的推送/渠道/停用用户不发、期号按 worker 时区；退避（精确的下次重试时间）、次数上限后 `failed`、不可重试错误立即失败、`Retry-After`、逐条模式断点续发、租约防重复、渠道互不影响、一条崩溃不影响其他、用户之间内容和渠道互不串、回收站条目不推、错误信息无密钥。
- 设置页/接口：密钥加密落库（解密校验、JSON 里没有明文）、只返回后 4 位、省略保持/空串清除、校验、服务器默认机器人、用户间隔离、三个渠道的“发送测试”（成功、失败且错误脱敏、未配置 400、不存在渠道 404、限频）、设置页含各表单/按钮、管理端点仅管理员、管理页转义、CLI。
- worker：`tick()` 端到端（采集被替换，建期 → 同一次 tick 里推送到 Slack 模拟端点，二次 tick 不重复）；调度器多了 `push-dispatch` 每分钟任务。

另外用真实浏览器（Playwright/Chromium）手动验证过设置页：填 Webhook → 点“发送测试消息” → 先保存再发送 → 本机接收端收到 Slack 格式的 JSON，刷新后只显示地址后 4 位，页面无 JS 错误。

## 已知限制 / 后续

- 没对真实的 Telegram / SMTP 服务商 / 飞书 / 钉钉 / Slack / Discord 做过联调，只对照各平台文档的请求格式和响应约定，用模拟端点测；飞书卡片用的是 Horizon 文档里的 Card JSON 2.0 样例。
- 没有自定义请求头（如 `Authorization`）、没有 GET 型 Webhook；需要时可加。
- Webhook 解析主机后再由 httpx 连接，理论上存在 DNS 重绑定的时间差；要更严就在出口网络层限制。
- 期号按 worker 时区的日期，用户时区与它差得多时，推送会晚到日报生成之后（不会丢）；一人一天一期（没有多次出刊的重复推送）。
- 投递记录不自动清理（每次推送一行，体量很小）。
- 邮件没有退订链接/多语言主题；测试邮件主题带“(测试)”。
- 设置页没有 CSRF token（沿用 M3 的 SameSite=Lax 方案）。
