"""DEPTAI-267: push X posts that Grok Bot read into newsd (POST /api/admin/x-import/items), one batch per source.

    NEWSD_URL=http://host:port NEWSD_TOKEN=<admin api token> python3 scripts/x_import_push.py batches.json

batches.json: [{"source": "xa:<handle>" | "xq:<id>", "calls": 1, "items": [{"url", "text", "author", "created_at",
"likes", "reposts", "replies", "media": [{"url", "type", "width", "height", "video"?}]}]}]
Take the source list from GET /api/admin/x-import/wanted first; stop when the server answers 429 (daily cap).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main(path: str) -> int:
    base, tok = os.environ["NEWSD_URL"].rstrip("/"), os.environ["NEWSD_TOKEN"]
    for batch in json.load(open(path, encoding="utf-8")):
        req = urllib.request.Request(f"{base}/api/admin/x-import/items", data=json.dumps(batch).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                print(json.dumps(json.loads(r.read()), ensure_ascii=False))
        except urllib.error.HTTPError as e:
            print(f"{batch.get('source')}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
            if e.code == 429:
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
