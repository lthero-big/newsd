# newsd Wiki

newsd 是一个可自托管的多用户 AI 日报：每个人订阅自己的信息源和关键词，newsd 每天定时采集、去重、打分，生成个人日报并推送到 Telegram、邮件或 Webhook。功能介绍和截图见 [仓库首页](https://github.com/lthero-big/newsd)。

## 目录

| 页面 | 内容 |
| --- | --- |
| [架构](Architecture) | web 和 worker 两个进程、数据库、Vercel 上的定时接口 |
| [部署](Deployment) | Docker Compose（推荐）、本机直接运行、Vercel + Supabase（实验性）、上线前检查 |
| [升级](Upgrade) | 网页端一键更新和各部署方式的手动升级 |
| [备份与恢复](Backup) | 数据库和 `NEWSD_SECRET_KEY` 的备份与恢复 |
| [配置](Configuration) | 常用环境变量 |
| [使用](Usage) | 管理员和普通用户上手、命令行、API |
| [常见问题](FAQ) | 部署和运行中常见的问题 |
| [开发](Development) | 本地开发、测试和 CI |

更细的设计和部署文档在仓库的 [docs/](https://github.com/lthero-big/newsd/blob/main/docs) 目录。

> 本 Wiki 的源文件在仓库 [`docs/wiki/`](https://github.com/lthero-big/newsd/blob/main/docs/wiki)，用 `scripts/publish-wiki.sh` 同步到 GitHub Wiki；修改请改仓库里的文件。
