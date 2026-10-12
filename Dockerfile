# serendip: a discover-feed proxy in front of Yattee Server. Runs the API on 8080 and the
# mirrored-traffic ingest on 8081.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The image's own Python, never a downloaded one: the venv is copied into an
# identical base below and its interpreter symlink has to resolve there.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /build
# Installed from the lock, so the image runs the versions the tests ran.
# Dependencies are their own layer so a source change does not download them again.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-editable
COPY src ./src
RUN uv sync --locked --no-editable

FROM python:3.12-slim

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin serendip \
 && mkdir -p /data \
 && chown serendip:serendip /data

COPY --from=builder /opt/venv /opt/venv

ARG SERENDIP_VERSION=dev

# The root filesystem is read-only in the pod; /data is the writable volume.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERENDIP_VERSION=${SERENDIP_VERSION} \
    SERENDIP_DB_PATH=/data/serendip.db

# Fail at build time, not in the init container: the package imports from the venv copy
# (not a source tree) and its bundled migrations apply to a throwaway database.
RUN SERENDIP_DB_PATH=/tmp/selfcheck.db python -c "import serendip.server; from serendip.migrate import migrate; print('migrations', migrate('/tmp/selfcheck.db'))" \
 && rm -f /tmp/selfcheck.db*

WORKDIR /app
USER serendip
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["python", "-m", "serendip", "serve"]
