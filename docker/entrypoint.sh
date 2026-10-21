#!/bin/sh
# Fail early with a fix instead of an obscure "readonly database" when /data is not writable for the newsd user
# (volumes created by the older images, which ran as root).
set -e
if [ "$(id -u)" != "0" ] && [ -d /data ]; then
  db=/data/newsd.db
  if [ ! -w /data ] || { [ -e "$db" ] && [ ! -w "$db" ]; }; then
    echo "newsd: /data is not writable for uid $(id -u). Volumes created by older newsd images (which ran as root) need a one-time fix:" >&2
    echo "  docker compose run --rm --user 0 --entrypoint chown web -R 10001:10001 /data" >&2
    exit 1
  fi
fi
exec "$@"
