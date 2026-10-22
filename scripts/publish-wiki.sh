#!/usr/bin/env bash
# Sync docs/wiki/ to the GitHub Wiki (https://github.com/lthero-big/newsd/wiki).
# GitHub only creates the wiki git repo after the first page is saved in the web UI,
# so open the Wiki tab once and save any page before the first run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${WIKI_REMOTE:-git@github.com:lthero-big/newsd.wiki.git}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git clone --quiet "$REMOTE" "$TMP/wiki"
find "$TMP/wiki" -maxdepth 1 -type f -name '*.md' -delete
cp "$ROOT"/docs/wiki/*.md "$TMP/wiki/"
cd "$TMP/wiki"
git add -A
if git diff --cached --quiet; then echo "wiki already up to date"; exit 0; fi
git commit --quiet -m "Sync wiki from docs/wiki ($(git -C "$ROOT" rev-parse --short HEAD))"
git push --quiet origin HEAD
echo "wiki published"
