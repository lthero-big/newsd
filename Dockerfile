# syntax=docker/dockerfile:1
# newsd image: multi-stage, runs as an unprivileged user (uid 10001), data in /data.
#   docker build -t newsd .
# Slow or blocked PyPI? --build-arg PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
# Offline: --build-arg WHEELHOUSE=http://host:port/  (a plain directory listing of wheels, used with --no-index)
ARG PYTHON_VERSION=3.12

# ---- build: a self-contained virtualenv with newsd and its dependencies (Postgres driver included)
FROM python:${PYTHON_VERSION}-slim AS build
ARG PYPI_MIRROR=""
ARG WHEELHOUSE=""
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
# dependencies first, so a code change does not reinstall them
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['postgres']))" > requirements.txt \
 && pip install ${PYPI_MIRROR:+--index-url "$PYPI_MIRROR"} ${WHEELHOUSE:+--no-index --find-links "$WHEELHOUSE"} -r requirements.txt
COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY newsd ./newsd
RUN pip install ${PYPI_MIRROR:+--index-url "$PYPI_MIRROR"} ${WHEELHOUSE:+--no-index --find-links "$WHEELHOUSE"} --no-deps . \
 && python -c "import newsd, pathlib; p = pathlib.Path(newsd.__file__).parent; \
assert (p / 'web/static/v2/nd.js').exists() and (p / 'migrations/versions').is_dir(), 'package data missing'"

# ---- runtime
FROM python:${PYTHON_VERSION}-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="newsd" \
      org.opencontainers.image.description="Self-hosted, multi-user AI news daily" \
      org.opencontainers.image.source="https://github.com/lthero-big/newsd" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
RUN groupadd --system --gid 10001 newsd \
 && useradd --system --uid 10001 --gid newsd --home-dir /app --no-create-home --shell /usr/sbin/nologin newsd \
 && mkdir -p /app /data && chown newsd:newsd /data
COPY --from=build /opt/venv /opt/venv
COPY config /app/config
COPY docker/entrypoint.sh /usr/local/bin/newsd-entrypoint
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEWSD_DATABASE_URL=sqlite:////data/newsd.db \
    NEWSD_IN_DOCKER=1
WORKDIR /app
USER newsd
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["newsd-entrypoint"]
# no HEALTHCHECK here: the worker runs the same image and serves no HTTP (compose sets one for web)
CMD ["newsd", "serve", "--host", "0.0.0.0", "--port", "8080"]
