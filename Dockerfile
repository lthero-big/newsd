# syntax=docker/dockerfile:1
# newsd image: multi-stage, dependencies and code in separate layers (a release pulls only the code layer), runs as an unprivileged user (uid 10001), data in /data.
#   docker build -t newsd .
# Slow or blocked PyPI? --build-arg PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
# Offline: --build-arg WHEELHOUSE=http://host:port/  (a plain directory listing of wheels, used with --no-index)
ARG PYTHON_VERSION=3.12

# ---- reqs: the dependency list only, so a version bump or code change in pyproject.toml's other fields
# leaves requirements.txt byte-identical and the dependency layer below stays cached
FROM python:${PYTHON_VERSION}-slim AS reqs
WORKDIR /src
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(sorted(set(p['dependencies'] + p['optional-dependencies']['postgres']))))" > requirements.txt

# ---- deps: a virtualenv with the third-party dependencies only (~200 MB, changes only when they change)
FROM python:${PYTHON_VERSION}-slim AS deps
ARG PYPI_MIRROR=""
ARG WHEELHOUSE=""
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY --from=reqs /src/requirements.txt /tmp/requirements.txt
# BuildKit cache mount: rebuilds reuse downloaded wheels; the cache is not part of the image
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ${PYPI_MIRROR:+--index-url "$PYPI_MIRROR"} ${WHEELHOUSE:+--no-index --find-links "$WHEELHOUSE"} -r /tmp/requirements.txt

# ---- app: newsd itself (a few MB) installed into its own directory, so a release only changes this small layer
FROM deps AS app
WORKDIR /src
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY newsd ./newsd
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install ${PYPI_MIRROR:+--index-url "$PYPI_MIRROR"} ${WHEELHOUSE:+--no-index --find-links "$WHEELHOUSE"} --no-deps --target /opt/newsd . \
 && cd / && PYTHONPATH=/opt/newsd python -c "import newsd, pathlib; p = pathlib.Path(newsd.__file__).parent; \
assert str(p).startswith('/opt/newsd/'), p; \
assert (p / 'web/static/v2/nd.js').exists() and (p / 'migrations/versions').is_dir(), 'package data missing'" \
 && head -1 /opt/newsd/bin/newsd | grep -qx '#!/opt/venv/bin/python'

# ---- runtime
FROM python:${PYTHON_VERSION}-slim
RUN groupadd --system --gid 10001 newsd \
 && useradd --system --uid 10001 --gid newsd --home-dir /app --no-create-home --shell /usr/sbin/nologin newsd \
 && mkdir -p /app /data && chown newsd:newsd /data
# dependencies (rarely change) first, newsd code (changes every release) last, so a release pulls ~3 MB
COPY --from=deps /opt/venv /opt/venv
COPY config /app/config
# the deploy compose file, so a server needs no checkout: docker run --rm lthero1/newsd cat /app/docker-compose.yml > docker-compose.yml
COPY docker-compose.yml /app/docker-compose.yml
COPY docker/entrypoint.sh /usr/local/bin/newsd-entrypoint
COPY --from=app /opt/newsd /opt/newsd
ENV PATH=/opt/newsd/bin:/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/newsd \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEWSD_DATABASE_URL=sqlite:////data/newsd.db \
    NEWSD_IN_DOCKER=1
WORKDIR /app
USER newsd
VOLUME /data
EXPOSE 8080
# last, so the per-release VERSION does not invalidate the layers above
ARG VERSION=dev
LABEL org.opencontainers.image.title="newsd" \
      org.opencontainers.image.description="Self-hosted, multi-user AI news daily" \
      org.opencontainers.image.source="https://github.com/lthero-big/newsd" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENTRYPOINT ["newsd-entrypoint"]
# no HEALTHCHECK here: the worker runs the same image and serves no HTTP (compose sets one for web)
CMD ["newsd", "serve", "--host", "0.0.0.0", "--port", "8080"]
