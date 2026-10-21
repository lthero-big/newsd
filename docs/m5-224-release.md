# M5 公开发布准备（DEPTAI-224）

目标：把私有仓库整理到“随时可以公开”的状态。**这一步没有公开仓库、没有改 GitHub 设置、没有改写历史**；是否公开由负责人决定，见 [public-release-checklist.md](public-release-checklist.md)。

## 做了什么

| 项 | 内容 |
|---|---|
| 许可 | `LICENSE`（MIT）；`pyproject.toml` 改为 `license = "MIT"` + `license-files`（需要 setuptools≥77，已写进 build-system）；Dockerfile 把许可文件带进镜像 |
| 第三方 | [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)：依赖许可清单和 Horizon（MIT）声明 |
| 文档 | README 重写（简介、功能、快速开始、配置、许可）；[deployment.md](deployment.md) / [deployment.en.md](deployment.en.md)（docker compose、裸机 systemd、HTTPS、Postgres、备份、排障） |
| 示例配置 | `.env.example` 补全（RSSHUB_*、NEWSD_GITHUB_TOKEN 等），全部为占位值；`config/*.json` 是公开默认目录，无密钥 |
| CI | `.github/workflows/ci.yml`：ruff、pytest（Python 3.11 / 3.12）、gitleaks（工作树 + 全部历史） |
| Lint | `pyproject.toml` 里的 `[tool.ruff]`；修掉全部 F401/F841/E401/E402 类问题 |
| 扫描 | 工作树和全部 12 个提交的隐私 / 密钥扫描，见下 |
| 清理 | `config/collect-sources.json` 里一条备注写着个人机器上的 `~/.config/news-daily/env` 路径，已改成通用说明；爬虫 User-Agent 里的 `news-daily-*` 和一个无效仓库网址改成 `newsd-*` |

## 许可选择：MIT

- 全部运行时依赖都能与 MIT 共存：trafilatura / htmldate / courlan 是 Apache-2.0；psycopg（仅 `postgres` 可选扩展）是 LGPL-3.0，以普通 import 动态使用、不修改不打包，默认镜像里没有；certifi（MPL-2.0）、tld（MPL-1.1 / GPL-2.0 / LGPL-2.1+ 任选）是文件级弱 copyleft，不对 newsd 自己的代码提出要求。没有 GPL/AGPL-only 的依赖，所以没有任何依赖强迫选 copyleft。
- 参考的 Horizon 是 MIT，选 MIT 最简单，声明沿用（见 THIRD_PARTY_NOTICES.md）。Horizon 的相关代码历史上是“参照 / 仿照”（LLM 客户端结构、`#{变量}` 模板语法、插件式采集、飞书卡片样例），没有发现整段拷贝，但保守起见仍保留其 MIT 声明。
- 版权人写成 “newsd contributors”，若要写具体个人 / 公司主体，公开前改 `LICENSE` 第三行即可（见检查清单）。
- 备选 Apache-2.0（带专利授权）或 AGPL-3.0（防止被做成闭源 SaaS）；没有被选中是因为项目目标是让大家随便自托管，且 AGPL 会吓退一部分企业用户。想改的话换 LICENSE 即可，依赖不阻止。

## 隐私与密钥扫描

**方法**（均在 `/tmp` 里临时安装，不进仓库）：

1. gitleaks 8.28.0（官方发行二进制）：`gitleaks git`（全部提交的全部 diff）和 `gitleaks dir`（工作树），默认规则集。
2. detect-secrets（pip）扫描所有被跟踪文件。
3. 对 `git log -p --all` 的全部新增行做正则检索：私有网段（10.x / 172.16-31.x / 192.168.x）、邮箱、全部出现过的域名、`lthero` / `zuvi` / `alex_wan` / `multica` / `cloudflare` / `pages.dev` / `internal` / `password=` / `secret=` 等关键词；并列出历史上出现过的所有文件名和被删除的文件，确认没有 `.env`、`data/`、`editions/`、数据库文件。

**结果**

- 工作树：**无真实密钥**。gitleaks 默认规则命中 3 处，全部是测试里明显的假 key（`sk-live-abcdefghijklmnop1234`、`sk-test-0123456789abcdef`、`sk-alice-12345678`，在 `tests/llm/` 和 `tests/pages/`），已在 `.gitleaks.toml` 里按路径 + 模式加了白名单，之后 `gitleaks dir` 和 `gitleaks git` 都是 0 命中。detect-secrets 的命中同样只有测试夹具（测试账号的 `ginapass123` 这类密码、`user:pw@a.example` 这类 URL、假 key）和 `.env.example` 里的 `change-me-...` 占位。
- 历史（12 个提交，diff 总量约 0.8 MB）：**无密钥、无需改写历史**。
  - 私有网段出现的位置全是 SSRF 防护的测试用例（`10.0.0.5`、`192.168.1.9`、`172.16.0.4`、`169.254.169.254`、`metadata.google.internal` 等）。
  - 所有邮箱都是 `*@example.com/.org`；出现的域名除公开站点外都是 `*.example`。
  - 历史里曾有 `~/.config/news-daily/env` 这个路径（上面已在当前树里清掉），只是路径，不含内容；历史里仍有，不构成泄露，**不建议为此改写历史**。
  - 被删过的文件：`newsd/collector/x_from_connector.py`、`newsd/web/templates/home.html`，已逐条查看其新增行，只有公开的 CDN 域名和解析逻辑。
- 需要负责人知晓的一项（元数据，不是密钥）：**所有 12 个提交的 author / committer 邮箱是 `alex.wan@zuvi.com`（公司邮箱）**。公开后会显示在 GitHub 提交里。要改只能重写历史（`git filter-repo --mailmap`）并强推，按任务要求我没有做；是否接受、或用 GitHub noreply 邮箱重写，由负责人决定。

## 代码检查（lint）

选 ruff，规则集 `E4, E7, E9, F`（pyflakes + 语法 + import 位置），行宽 140。代码库刻意用紧凑写法（`a = 1; b = 2`、短 lambda、`l`/`s` 循环变量），所以不开纯风格类的 E701 / E702 / E731 / E741，否则要重排上百行没有价值的改动。测试和 `scripts/` 允许 E402（需要先改 `sys.path`）。起初默认规则有 166 个问题，现在 0 个；修的都是未用的 import / 变量，改完 224 个测试全绿。

本地和 CI 同一条命令：`ruff check .`

## CI 怎么工作

`.github/workflows/ci.yml`，触发：push 到 `main`、所有 PR。

- **test**（Python 3.11、3.12 两个版本）：`pip install -e '.[dev]'` → `ruff check .` → `pytest -q`。测试全用模拟 HTTP，不依赖外网和密钥，所以 fork 的 PR 也能跑。
- **secret-scan**：`fetch-depth: 0` 取全历史，下载固定版本（8.28.0）的 gitleaks，跑 `gitleaks git` 和 `gitleaks dir`，配置读仓库根的 `.gitleaks.toml`（默认规则 + 测试夹具白名单）。
- 权限 `contents: read`，没有任何 secret 依赖。

本地验证过：Python 3.11 全新 venv 从 `pyproject.toml` 安装，`ruff check .` 通过、224 个测试全绿；Docker 镜像构建通过，容器里 `/healthz` 正常、`admin create` 和 `db import-config`（24 个源、89 个标签词）正常，包元数据里 `License-Expression: MIT`。

## 已知限制

- 仓库是私有的、本环境没有 `gh`，**没法查看 GitHub 上 CI 的实际运行结果**（未认证访问私有仓库返回 404）；推送后请在 Actions 页确认第一次运行。
- 没做屏幕截图（README 里留了注释位置）。
- 飞书 / 钉钉等推送没对真实服务联调过（M4 文档已说明）。
