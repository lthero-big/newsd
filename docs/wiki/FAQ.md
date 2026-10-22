# 常见问题

**不配大模型能用吗？**
能。日报按关键词规则打分和去重；配上模型后多了 AI 打分、语义去重、翻译和摘要。

**SQLite 还是 Postgres？**
个人和小团队用 SQLite 足够（web 和 worker 共用一个文件，已开启 WAL）。用户多、写入频繁，或者部署在 Vercel 这类无状态平台上时，用 Postgres。

**Vercel 上日报没有按时出 / `/api/cron/tick` 返回 401 或 503？**
503 表示没有设置 `CRON_SECRET`（接口默认关闭）；401 表示调用方带的密钥不对，Vercel Cron 会自动带 `Authorization: Bearer $CRON_SECRET`，GitHub Actions 需要把同一个值存成仓库 Secret。Hobby 版每天只触发一次且有最多 59 分钟的随机延迟；运行超过函数时长上限会被平台中止，在「日志与任务」里显示为「中断」。见 [docs/vercel.md](https://github.com/lthero-big/newsd/blob/main/docs/vercel.md)。

**换了 `NEWSD_SECRET_KEY` 会怎样？**
已保存的模型 key 和推送密钥都无法解密，所有人需要重新填写，已登录的会话也会失效。请把它和数据库一起备份。

**X（Twitter）的源抓不到？**
X 和部分国内源需要自建 RSSHub，设置 `RSSHUB_BASE`（和 `RSSHUB_ACCESS_KEY`）后才会抓取，未设置时这些源会被跳过。

**推送到内网 Webhook 被拒绝？**
出于 SSRF 防护，默认不允许推送到内网和回环地址；确实需要时设置 `NEWSD_PUSH_ALLOW_PRIVATE=1`。

**日报什么时候生成？为什么今天还没有？**
worker 在 `NEWSD_WORKER_TIMES` 指定的时刻（`NEWSD_WORKER_TZ` 时区）运行。确认 worker 进程在跑，然后在「管理 → 日志与任务」看最近一次运行的时间线和错误；也可以在那里手动运行一次。

**Docker 里提示 `/data is not writable`？**
旧版镜像以 root 运行，新镜像改为非 root（uid 10001）。执行一次 `docker compose run --rm --user 0 --entrypoint chown web -R 10001:10001 /data` 修正数据卷的属主。

**出现 `database is locked`？**
SQLite 被多个进程同时写入时偶发，连接已设置 30 秒超时；写入压力大时换成 Postgres。
