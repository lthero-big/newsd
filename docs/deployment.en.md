# Deployment guide

中文: [deployment.md](deployment.md)

newsd is two processes sharing one database:

- **web**: `newsd serve`, FastAPI on port 8080. Runs database migrations on start-up.
- **worker**: `newsd worker start`, APScheduler. At each `NEWSD_WORKER_TIMES` it collects the shared pool once, builds every user's issue, and sends due pushes.

No Redis, no queue, no GitHub Actions or Cloudflare needed: Python 3.11+ (or Docker) and a host with outbound internet.

## Required configuration

Copy `.env.example` to `.env` and set **at least**:

```bash
NEWSD_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
```

- `NEWSD_SECRET_KEY` encrypts users' model keys and push secrets and signs sessions. **Never change or lose it after first use** — stored secrets become undecryptable and users must re-enter them. Back it up with the database.
- Set `NEWSD_COOKIE_SECURE=1` once you serve over HTTPS.
- `NEWSD_WORKER_TIMES` / `NEWSD_WORKER_TZ`: when the daily issue is built, e.g. `07:00` + `Europe/Berlin`.
- `NEWSD_PUBLIC_URL`: public base URL, used for the "full report" link in pushed messages.
- Everything else (SMTP, default Telegram bot, model gateway, push retries, …) is documented in the comments of `.env.example`.

## Option 1: docker compose

```bash
git clone https://github.com/lthero-big/newsd.git && cd newsd
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose exec web newsd create-admin admin          # prompts for a password
docker compose exec web newsd db import-config \
  --sources /app/config/collect-sources.json --tags /app/config/tags.json
docker compose logs -f worker
```

- `web` and `worker` share one image and the named volume `newsd-data` (mounted at `/data`, SQLite file `/data/newsd.db`). `worker` starts after `web` is healthy.
- Port `8080` is published on the host. On a public host expose only the reverse proxy: change the mapping to `127.0.0.1:8080:8080`.
- Run the whole pipeline once, right now: `docker compose exec worker newsd worker run-once --no-ai`.
- Upgrade: `git pull && docker compose up -d --build` (migrations run when web starts).

### Postgres

The image has no Postgres driver by default. In the `Dockerfile` change `pip install --no-cache-dir .` to `pip install --no-cache-dir '.[postgres]'`, add a `postgres` service to the compose file, and set

```bash
NEWSD_DATABASE_URL=postgresql+psycopg://newsd:CHANGE_ME@db:5432/newsd
```

Also remove the `NEWSD_DATABASE_URL` override under `environment:` in the compose file. psycopg is LGPL-3.0, see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Option 2: bare metal (systemd)

```bash
sudo useradd -r -m -d /opt/newsd newsd
sudo -u newsd git clone https://github.com/lthero-big/newsd.git /opt/newsd/app
cd /opt/newsd/app
sudo -u newsd python3.11 -m venv .venv          # 3.11 or newer
sudo -u newsd .venv/bin/pip install .            # use '.[postgres]' for Postgres
sudo -u newsd cp .env.example .env && sudo -u newsd $EDITOR .env
# point NEWSD_DATABASE_URL at an absolute path, e.g. sqlite:////opt/newsd/data/newsd.db
sudo -u newsd .venv/bin/newsd db upgrade
sudo -u newsd .venv/bin/newsd create-admin admin
sudo -u newsd .venv/bin/newsd db import-config --sources config/collect-sources.json --tags config/tags.json
```

`/etc/systemd/system/newsd-web.service`:

```ini
[Unit]
Description=newsd web
After=network-online.target

[Service]
User=newsd
WorkingDirectory=/opt/newsd/app
ExecStart=/opt/newsd/app/.venv/bin/newsd serve --host 127.0.0.1 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/newsd-worker.service`: same, with `Description=newsd worker`, `ExecStart=/opt/newsd/app/.venv/bin/newsd worker start` and `After=newsd-web.service`.

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now newsd-web newsd-worker
```

`.env` is read from the working directory (or set `NEWSD_ENV_FILE`).

## Reverse proxy and HTTPS

The web process does not terminate TLS. Put Caddy or nginx in front, forward to `127.0.0.1:8080`, and set `NEWSD_COOKIE_SECURE=1` and `NEWSD_PUBLIC_URL=https://your.domain`. Caddy:

```
news.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

## Registration and first users

Default `NEWSD_REGISTRATION=invite`: the admin runs `newsd admin invite` and hands out single-use codes. Switch to `open` for a public site or `closed` to stop self-service sign-up; at runtime: `newsd admin registration open`.

## Backup and restore

- SQLite: stop the worker and copy `newsd.db` (plus `-wal`/`-shm` if present), or take a live copy with `sqlite3 newsd.db ".backup backup.db"`.
- Postgres: `pg_dump`.
- **Back up `NEWSD_SECRET_KEY` together with the database** — one is useless without the other.

## Troubleshooting

- `GET /healthz` returning 200 means web is up.
- Admin page "Worker runs" and `newsd worker runs` show each pipeline run's stages, per-user results and errors; "Push records" and `newsd push list` show delivery results.
- Webhooks to private addresses are refused by default (SSRF protection); set `NEWSD_PUSH_ALLOW_PRIVATE=1` if you really need them.
- `database is locked`: occasional with SQLite when several processes write; the connection timeout is already 30 s. Switch to Postgres under heavy write load.
