# DEPTAI-216 (2)：多服务商模型、优先级与故障切换

按已验收的 v2 demo（`design/demo-v2` 的模型设置页）实现。设置按用户隔离（模型页在“个人”分组）。

## 预设（`newsd/llm/presets.py`）

| 地区 | 预设 id |
| --- | --- |
| 国外 | `openai` `anthropic` `gemini` `xai`(Grok) `mistral` `groq` `openrouter` |
| 国内 | `deepseek` `qwen`(百炼) `glm`(智谱) `kimi`(Moonshot) `doubao`(火山方舟) `qianfan`(文心) `hunyuan` `minimax` `stepfun` `yi` `siliconflow` |
| 本地 / 自定义 | `ollama`（无需 key）`custom`（任意 OpenAI 兼容地址，必须填 Base URL） |

每个预设带协议（OpenAI `/chat/completions`；Anthropic `/v1/messages`；Gemini 原生 `generateContent`；Ollama `/api/chat`）、默认 Base URL、常用模型和 key 提示。
旧 id `tongyi`、`openai_compatible` 保留为隐藏别名，已有配置和 v1 接口继续可用。管理员仍可用 `NEWSD_LLM_BASE_<ID>` 改某个预设的默认地址。

## 存储

- 新表 `llm_providers`（迁移 `0009_216_providers`）：每行一个服务商：名称、预设、可选 Base URL（NULL = 用预设默认）、模型、加密的 key（服务器主密钥 Fernet）、key 末 4 位、启用、优先级、最近一次测试结果。
- `user_settings` 里的 `llm` 只留选项：`translate_to`、`retries`（每个服务商失败重试 0–3 次，默认 2）、`timeout`（10–300 秒，界面提供 30/60/120，默认 60）。
- 迁移把旧的单一配置搬成一行（密文原样搬，不解密），降级时把第一行搬回。

## 故障切换

`client_for_user()` 返回 `FailoverClient`：按优先级排列已启用且可用的服务商。每个请求先发给第一个没在冷却中的服务商。它（自己的重试用完后）失败时冷却 5 分钟，请求转到下一个；全部失败时抛出最后一个错误。
报告里的 `usage` 多了 `failovers` 和 `chain` 两个字段。

## 自定义 Base URL 的 SSRF 防护（`newsd/llm/netguard.py`）

- 只允许 http/https，不得带账号密码、query 或 fragment。
- 主机解析出的每个地址都必须是公网地址。回环、私网、链路本地（含 169.254.169.254）、CGNAT、组播、保留地址和 IPv4-mapped 一律拒绝。
- 例外：管理员账号，或服务器设置了 `NEWSD_LLM_ALLOW_PRIVATE_URLS=1`。
- 保存和测试时检查一次，每次请求前再检查一次，防 DNS rebinding。
- 不跟随重定向，不用环境代理。

## 接口

- `GET /api/llm/presets`
- `GET /api/me/llm/providers`：列表、故障切换链和选项
- `POST /api/me/llm/providers`
- `PATCH /api/me/llm/providers/{id}`：不带 `api_key` 保留原 key，`""` 删除；`base_url: ""` 恢复预设默认；改预设时自定义地址自动清空
- `DELETE /api/me/llm/providers/{id}`
- `PUT /api/me/llm/order`：`{"ids": [...]}`，必须列出全部服务商
- `PUT /api/me/llm/options`
- `POST /api/me/llm/providers/test`：测试表单里的值，不保存；带 `provider_id` 且预设没变时复用已存的 key
- `POST /api/me/llm/providers/{id}/test`：测试已保存的服务商，结果记在卡片上
- 测试连接两个接口共用限流：每人每分钟 6 次，不重试，超时 20 秒。
- v1 的 `/api/me/llm`（GET/PUT/DELETE/test）照常可用，作用于主用服务商（优先级第一个）。

## 界面

设置页“模型”卡片：服务商列表（启用开关、↑↓ 或拖拽排序、测试、编辑、删除）、故障切换链预览（主用 → 备份1 → …）、添加/编辑表单（显示名称、预设、Base URL 自动填入可改、模型下拉 + 自由输入、API Key 只显示末 4 位、测试连接），以及全局选项（失败重试、超时、翻译语言）。
完整的 v2 界面改版是 216 的第 3 项。
