# 私有 → 公开 检查清单

状态日期：2026-10-21（DEPTAI-224）。每项后面是证据。**最后一项“公开仓库”在负责人明确确认前保持未勾选。**

## 许可与声明

- [x] 选定并添加 LICENSE（MIT）。证据：`LICENSE`；`docs/m5-224-release.md` “许可选择”；包元数据 `License-Expression: MIT`（Docker 内验证）。
- [x] 依赖许可盘点，无 GPL/AGPL-only 依赖，Apache-2.0（trafilatura / htmldate / courlan）和 LGPL（可选 psycopg）已说明。证据：`THIRD_PARTY_NOTICES.md`。
- [x] 参考项目 Horizon（MIT）的声明已保留。证据：`THIRD_PARTY_NOTICES.md` 顶部。
- [ ] 负责人确认版权人名称（现为 “newsd contributors”）是否要改成具体个人 / 公司。证据：`LICENSE` 第 3 行。**待负责人**。

## 隐私与密钥

- [x] 工作树扫描无真实密钥。证据：`gitleaks dir` 0 命中（测试假 key 已按路径白名单，`.gitleaks.toml`）；detect-secrets 只命中测试夹具。
- [x] 全部 git 历史（12 个提交）扫描无密钥，无需改写历史。证据：`gitleaks git` 0 命中；`git log -p --all` 正则检索结果见 `docs/m5-224-release.md`。
- [x] 没有私有 IP / 内部主机名 / 私有端点 / 个人路径留在当前树里。证据：检索结果中的私网地址均为 SSRF 测试用例；`config/collect-sources.json` 里的个人路径备注已清除。
- [x] `.env`、数据库、`var/`、`data/`、`editions/` 从未进入历史，且在 `.gitignore` 里。证据：`git log --all --name-only` 文件清单；`.gitignore`。
- [x] `.env.example` 和 `config/*.json` 只含占位值 / 公开网址。证据：人工逐项检查 + gitleaks。
- [ ] 提交作者邮箱 `alex.wan@zuvi.com`（公司邮箱，12 个提交）是否可接受公开；如不接受需重写历史（`git filter-repo --mailmap`）并强推。**待负责人决定**（任务规定不得自行重写历史）。

## 文档

- [x] README：简介、功能、快速开始、配置、测试、许可。证据：`README.md`。
- [x] 部署文档（docker compose + 裸机 systemd），中英文。证据：`docs/deployment.md`、`docs/deployment.en.md`；Docker 镜像构建 + 容器内 `/healthz`、`admin create`、`db import-config` 实测通过。
- [x] 示例配置无真实值。证据：`.env.example`。
- [ ] README 截图。可选，暂用注释占位。

## 质量与 CI

- [x] 全部测试通过。证据：`pytest` 224 passed（Python 3.12 本地、Python 3.11 全新 venv 各一次）。
- [x] Lint 通过，命令与 CI 一致。证据：`ruff check .` → All checks passed。
- [x] CI 工作流已添加（ruff + pytest 3.11/3.12 + gitleaks）。证据：`.github/workflows/ci.yml`。
- [ ] CI 在 GitHub 上首次运行为绿。证据缺失：本环境无 `gh` 且仓库私有，无法查看。**推送后请在 Actions 页确认**。

## 公开

- [ ] **把仓库改为 public（需要负责人明确确认后，由负责人在 GitHub Settings → Danger Zone 操作；本任务未执行）。**

公开前后建议顺手做的（不在本任务范围）：开启 GitHub secret scanning 和 push protection、Dependabot；在 Settings 里设 About 描述和 topics；为 `main` 开分支保护要求 CI 通过。
