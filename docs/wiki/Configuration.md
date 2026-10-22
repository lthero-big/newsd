# 配置

所有配置都通过环境变量设置，写在 `.env` 里；[.env.example](https://github.com/lthero-big/newsd/blob/main/.env.example) 列出了全部选项和说明。常用的有：

| 变量 | 作用 |
| --- | --- |
| `NEWSD_SECRET_KEY` | **必填**，加密用户密钥和签发会话的主密钥 |
| `NEWSD_DATABASE_URL` | 数据库地址，默认 `sqlite:///./var/newsd.db`；Postgres 用 `postgresql+psycopg://user:pass@host:5432/db` |
| `NEWSD_REGISTRATION` | 默认注册方式：`invite`、`open` 或 `closed`（运行中可在后台切换） |
| `NEWSD_COOKIE_SECURE` | 在 HTTPS 后面时设为 `1` |
| `NEWSD_PUBLIC_URL` | 对外访问地址，推送消息里的「查看完整日报」链接用它 |
| `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ` | 每天出日报的时刻和时区，例如 `07:00` 和 `Asia/Shanghai` |
| `NEWSD_SMTP_*` | 邮件推送用的 SMTP 服务器 |
| `NEWSD_TELEGRAM_BOT_TOKEN` | 可选，给没有填自己 bot 的用户用的默认 Telegram bot |
| `RSSHUB_BASE` / `RSSHUB_ACCESS_KEY` | 自建 RSSHub 地址；不设时 X 等依赖 RSSHub 的源会跳过 |
| `NEWSD_GITHUB_TOKEN` | 可选，提高 GitHub 源和版本检查的接口限额 |
| `NEWSD_UPDATE_CHECK` | 设为 `0` 关闭版本检查；`NEWSD_DEPLOY_MODE=none` 关闭网页端更新 |
| `NEWSD_RESTART_CMD` | 本机部署网页端更新后的重启命令，例如 `sudo systemctl restart newsd-web newsd-worker`；不设时进程自己重启 |

`config/collect-sources.json` 和 `config/tags.json` 是默认的公开信息源目录和标签词表，导入后可以在网页上继续增删。
