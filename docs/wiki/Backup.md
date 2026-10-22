# 备份与恢复

需要备份的只有两样东西，**缺一不可**：

1. **数据库**
   - SQLite（默认）：在线备份 `sqlite3 var/newsd.db ".backup newsd-backup.db"`；Docker 里的文件在卷 `newsd-data` 的 `/data/newsd.db`，可以用
     `docker compose exec web python -c "import sqlite3; s=sqlite3.connect('/data/newsd.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"`
     再 `docker compose cp web:/data/backup.db .` 拷出来。
   - Postgres / Supabase：`pg_dump "$NEWSD_DATABASE_URL" > newsd.sql`（连接串去掉 `+psycopg`）。Supabase 请用 **Session pooler**（端口 5432）或直连地址做备份，不要用 6543 的事务连接池；免费版 Supabase 也有每日自动备份。
2. **`NEWSD_SECRET_KEY`**：存到密码管理器里。没有它，备份里加密保存的模型 key 和推送密钥都无法解密。

恢复：停掉 web 和 worker，把备份放回原位置（或 `psql < newsd.sql`），确认 `.env` 里是**同一个** `NEWSD_SECRET_KEY`，再启动。
