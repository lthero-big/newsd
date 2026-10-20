# M2-4 模型层（DEPTAI-219）

## 改了什么

- **多服务商 LLM 客户端** `newsd/llm/client.py`：一套接口 `LLMClient.complete()` / `complete_json()` / `test_connection()`，参照 Horizon 的 ai client。配置只有三项：服务商、模型、key（`ModelConfig(provider, model, api_key)`）。
  | provider | 协议 | 默认地址 |
  |---|---|---|
  | `openai` | OpenAI `/chat/completions` | api.openai.com/v1 |
  | `deepseek` | OpenAI 兼容 | api.deepseek.com/v1 |
  | `tongyi`（通义千问） | OpenAI 兼容 | dashscope.aliyuncs.com/compatible-mode/v1 |
  | `doubao`（豆包/火山方舟） | OpenAI 兼容 | ark.cn-beijing.volces.com/api/v3 |
  | `openai_compatible` | OpenAI 兼容 | 由管理员设 `NEWSD_LLM_BASE_OPENAI_COMPATIBLE` |
  | `anthropic` | `/v1/messages`（`x-api-key` + `anthropic-version`） | api.anthropic.com |
  | `gemini` | `/v1beta/models/{model}:generateContent`（key 走 `x-goog-api-key` 头，不进 URL） | generativelanguage.googleapis.com |
  | `ollama` | `/api/chat`，无需 key | localhost:11434 |
  - 用户不能填 URL：地址按服务商固定，管理员可用 `NEWSD_LLM_BASE_<PROVIDER>` 覆盖（网关、Ollama 所在机器）。这样用户配置不可能让服务器去请求内网地址（与 M2-2 的 SSRF 思路一致）。
  - 限速：`NEWSD_LLM_RPM`（默认每分钟 30 次，按请求开始时间均匀间隔）；并发上限：`NEWSD_LLM_CONCURRENCY`（默认 3）；超时 60 秒。
  - 重试：超时、连接错误、408/409/425/429/5xx/529 指数退避重试（默认 3 次，尊重 `Retry-After`，上限 30 秒）；401/403（auth）和其他 4xx 不重试。
  - JSON：先宽松解析（纯 JSON、```json 代码块、最外层 `{…}`/`[…]`）；不合法就让模型修一次；还不合法返回 `None`，调用方记为空结果，不影响整轮。
  - 错误信息和 `repr` 里不会出现 key。
- **用户 key 加密存储** `newsd/llm/keys.py`：服务商/模型/翻译目标语言存在 `user_settings`（key=`llm`），API key 用 M1 的服务器主密钥 Fernet（`security.encrypt_secret`）加密后存 `user_settings.secret`。API 永不返回 key，只给后 4 位（key 太短时连后 4 位也不给）。主密钥换了解不开时按“未配置”处理并标记 `key_unreadable`。
- **设置页和接口**：`/settings` 页面（模型表单 + “测试连接”按钮 + 内容类别勾选和阈值）。
  - `GET /api/llm/providers`
  - `GET/PUT/DELETE /api/me/llm`（PUT 不带 `api_key` = 保留原 key，`""` = 删除）
  - `POST /api/me/llm/test`：用表单里的值测试（不保存），缺的字段用已保存的；已保存的 key 只会发给保存时的那个服务商；不重试、超时 20 秒；每用户每分钟最多 6 次。
  - `GET /api/profiles`，`GET/PUT /api/me/profiles`
- **处理配置（profiles）** `newsd/llm/profiles.py` + `newsd/llm/profiles/*.md`：每个内容类别一个 Markdown 提示词，含三段——是否属于本类、0–10 打分标准、输出栏目，front matter 里是默认阈值和栏目列表。内置 5 个：`ai-news`（AI 资讯，6）、`research`（论文与研究，7）、`open-source`（开源与发布，6）、`creative-ai`（图片视频音频生成，6）、`agents`（Agent 与开发工具，6）。用户可多选、各自调阈值；未设置时默认 `ai-news`。管理员可用 `NEWSD_PROFILES_DIR` 加自定义类别。
- **共享翻译缓存**（迁移 `0004_m2_llm`，表 `translation_cache`）：按 目标语言 + 规范化原文 的 sha256 去重，所有用户共享，记录产出模型和命中次数。只把缓存未命中的文本分批（20 条一批）交给模型；中文标题翻成中文时自动跳过。没有 key 的用户也能读到缓存里已有的译文，但不会触发任何模型调用。
- **处理管线** `newsd/llm/process.py`，命令 `newsd process run <用户名> [--since-hours 26] [--max-items N] [--no-ai] [--no-rerank] [--no-dedupe] [--no-translate]`：
  1. 规则排序（不需要 key）：屏蔽词剔除，兴趣/加权词加分，按时间衰减。
  2. 有 key 时，每个选中的类别分批（8 条一批）打分，结果存 `item_scores`（每用户、每条目、每类别一行；`ok` 的下次复用，`empty`/`error:*` 下次重试）。
  3. 任一类别判定属于且分数 ≥ 用户阈值才入选，取最高分的类别和栏目。
  4. 可选语义去重：模型把同一事件的报道分组，每组留分数最高的，其余记到 `duplicates`。
  5. 可选 AI 重排（前 30 条）；回复不合法就保持原顺序。
  6. 翻译标题和摘要（走共享缓存）。
  - 没有 key：只做第 1 步和缓存翻译，零模型调用，管线照常出结果。
  - 模型回复两次都不是合法 JSON：这批记为 `empty`，继续下一批；如果整轮一个可用结果都没有，退回规则排序。
  - 401/403：本轮停止后续模型调用（换了错 key 不会刷一堆请求），退回规则排序。

## 测试

`.venv/bin/pytest`：131 个通过（M2-2 基线 102 个 + 新增 29 个），全部用 `httpx.MockTransport` 模拟响应，没有真实 API 调用（`tests/llm/conftest.py` 默认把用户客户端的 transport 换成“一调用就报错”的假 transport）。

- `tests/llm/test_client.py`：OpenAI 请求/响应格式、DeepSeek/通义/豆包地址和 json 模式、管理员配置的 OpenAI 兼容地址、**Anthropic** 请求头和请求体、多段 content 拼接、```json 代码块、401 不重试且错误里不含 key、Gemini/Ollama 格式、配置校验、429 按 `Retry-After` 等待、503 指数退避、重试用尽、超时重试、400 不重试、非法 JSON 修复一次成功、修复后仍非法返回 None（恰好 2 次请求）、并发上限（8 个请求同时最多 2 个在飞）、限速（rpm=30 时每次间隔 2 秒）、测试连接不重试。
- `tests/llm/test_api.py`：key 加密落库（库里没有明文，能用主密钥解开）、接口不回显 key、改模型不重填 key、非法输入不留半截数据、用户隔离；测试连接（OpenAI 成功、Anthropic 401、用已保存配置、已保存 key 不会发给别的服务商、限流 429）；类别列表/选择/阈值校验；设置页渲染和未登录跳转。
- `tests/llm/test_process.py`：无 key 时零调用照常出结果（屏蔽词、加权、时间排序）；OpenAI 全流程（打分 → 阈值 → 语义去重 → 重排 → 翻译），第二轮复用打分和翻译缓存；按用户阈值；Anthropic 返回非法 JSON → 记 empty、整轮不失败；401 立即停止并退回规则排序；翻译缓存跨用户共享（无 key 用户读缓存、有 key 用户只翻未命中的）；自动用加密保存的 key 构建客户端。

## 和 Horizon 的对比

- 相同：一个统一的客户端接口挂多个服务商；配置只要服务商 + 模型 + key；按类别写提示词让模型判断归属并打分，再按阈值筛选；AI 重排和翻译是可选步骤。
- 不同：
  - Horizon 是单用户，key 放在环境变量/配置文件里；newsd 多用户，每人自己的 key，用服务器主密钥加密存库，接口只回显后 4 位，并提供测试连接接口。
  - Horizon 的 base URL 可由配置随意指定；newsd 用户不能填 URL，只有管理员能通过环境变量改，避免用户配置被用来打内网。
  - 翻译缓存在 newsd 里是全站共享的，同一句英文标题全站只翻一次；无 key 用户也能读缓存。
  - 打分结果持久化到 `item_scores`，成功的复用、失败的下轮重试；Horizon 每轮重新打分。
  - 非法 JSON 统一“修一次，不行就记空”，不让单批失败拖垮整轮；无 key 时整条管线照常跑（只是规则排序）。
  - 我们没有直接调用各家 SDK，全部走 httpx，便于统一限速/重试/测试，也少装依赖。

## 已知限制 / 后续

- 语义去重用的是模型分组（一次最多 60 条），没有用 embedding；Anthropic 没有 embedding 接口，按服务商分叉会让行为不一致。条目很多时可以先按标题相似度预分桶再交给模型。
- 限速和并发上限是每个客户端实例（即每个用户每轮）各自的；多个用户共用同一个服务商账号时没有全站总限速。
- 豆包和自定义 OpenAI 兼容服务不发 `response_format`（并非所有模型都支持），只靠提示词 + 修复兜底。
- 测试只用模拟响应，没有对真实服务商做联调；上线前建议每家用测试连接按钮各试一次。
- 调度器、按用户配额、日报渲染属于后续子任务；这里提供 `process_user()` / `newsd process run` 供它们调用。
