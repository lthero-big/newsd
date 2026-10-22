# Third-party notices

newsd itself is released under the [MIT License](LICENSE). This file lists what it builds on.

## Horizon (MIT)

Parts of newsd were designed after [Horizon](https://github.com/Thysrael/Horizon): the multi-provider LLM client layout
(`newsd/llm/client.py`), the `#{variable}` webhook template syntax (`newsd/push/template.py`), the plugin-style fetchers
(`newsd/collector/plugins.py`), the Feishu card JSON sample used in the Feishu webhook template, and (DEPTAI-216) the
per-item summary step in `newsd/llm/digest.py` -- Horizon's analysis/enrichment approach of giving the model title,
source, URL, engagement and a head-middle-tail (40/30/30) excerpt, treating every item field as untrusted data, forbidding
invented facts, and asking for a localized title plus a short "what happened / why it matters" summary as JSON -- and the
"<repo> <tag>" title shape for GitHub release items in `newsd/content.py`. newsd batches items per request and caches the
result for all users, where Horizon analyses one item per call. As far as the history
shows these are re-implementations rather than verbatim copies, but because the ideas, wire formats and one JSON sample come from
Horizon, its notice is reproduced here:

```
MIT License

Copyright (c) 2026 Thysrael

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Python dependencies

Runtime dependencies as resolved on 2026-10-21 (`pip install '.[postgres]'` into a clean venv, listed with `pip-licenses`;
versions will drift, the licenses are what matter). Dependencies are installed from PyPI, not vendored or redistributed in this
repository. The Docker image installs the base package without the `postgres` extra.

Direct dependencies (from `pyproject.toml`):

| Package | License | Notes |
|---|---|---|
| fastapi | MIT | |
| uvicorn[standard] | BSD-3-Clause | pulls uvloop (MIT/Apache-2.0), httptools (MIT), watchfiles (MIT), websockets (BSD-3-Clause), python-dotenv (BSD-3-Clause), PyYAML (MIT) |
| sqlalchemy | MIT | |
| alembic | MIT | pulls Mako (MIT) |
| jinja2 | BSD-3-Clause | pulls MarkupSafe (BSD-3-Clause) |
| python-multipart | Apache-2.0 | |
| apscheduler (3.x) | MIT | pulls tzlocal (MIT), pytz (MIT) |
| cryptography | Apache-2.0 OR BSD-3-Clause | pulls cffi (MIT-0), pycparser (BSD-3-Clause) |
| httpx | BSD-3-Clause | pulls httpcore, h11 (MIT), anyio (MIT), certifi (MPL-2.0), idna (BSD-3-Clause) |
| trafilatura | Apache-2.0 | pulls htmldate (Apache-2.0), courlan (Apache-2.0), justext (BSD), lxml (BSD-3-Clause), lxml_html_clean (BSD-3-Clause), charset-normalizer (MIT), urllib3 (MIT), dateparser (BSD-3-Clause), tld (MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later), regex (Apache-2.0 AND CNRI-Python), python-dateutil (Apache-2.0 / BSD), six (MIT), babel (BSD-3-Clause) |
| tzdata | Apache-2.0 | |
| psycopg[binary] (optional `postgres` extra) | LGPL-3.0-only | see below |
| pytest, ruff (optional `dev` extra) | MIT | development only, not installed in the image |

Also present transitively through fastapi: starlette (BSD-3-Clause), pydantic / pydantic_core / annotated-types / typing-inspection (MIT),
typing_extensions (PSF-2.0), annotated-doc (MIT), click (BSD-3-Clause), opentelemetry-api (Apache-2.0).

### Licenses that need a word

- **trafilatura, htmldate, courlan (Apache-2.0)**: permissive. newsd only imports them; their license and NOTICE text ship with the wheels.
  Apache-2.0 is compatible with MIT-licensed code that merely depends on them.
- **psycopg / psycopg-binary (LGPL-3.0)**: only installed when the user asks for the `postgres` extra. newsd links to it dynamically
  as a normal Python import and does not modify or bundle it, which is what the LGPL allows for an MIT application.
  The default Docker image does not contain it.
- **certifi (MPL-2.0)** and **tld (MPL-1.1 / GPL-2.0 / LGPL-2.1+, a choice of licenses; MPL-1.1 applies)**: file-level weak copyleft,
  unmodified and not redistributed by this repository, so nothing is imposed on newsd's own code.
- No dependency is GPL/AGPL-only or otherwise forces a copyleft license on newsd, so MIT is available.

To regenerate: `python -m venv /tmp/lic && /tmp/lic/bin/pip install '.[postgres]' pip-licenses && /tmp/lic/bin/pip-licenses --format=markdown --with-urls`.
