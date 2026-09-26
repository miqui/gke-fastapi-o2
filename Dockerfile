# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for the FastAPI + SQLAlchemy REST API

# Stage 1: build the virtualenv. uv resolves strictly from uv.lock (--frozen), and the deps layer
# is separate from the app-code layer so editing app/ doesn't reinstall dependencies.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

# Stage 2: runtime. Same base image as the builder, so the venv's interpreter symlink resolves.
FROM python:3.13-slim

# Numeric uid/gid 10001: k8s/deployment.yaml sets runAsUser: 10001 + runAsNonRoot, and the kubelet
# can only verify "non-root" for a numeric USER. Above 10000 so it can't collide with a host user.
RUN groupadd -g 10001 app && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# COPY --chown avoids a `chown -R` layer that would duplicate the whole venv.
COPY --chown=app:app --from=builder /opt/venv /opt/venv
COPY --chown=app:app app/ app/
COPY --chown=app:app migrations/ migrations/
COPY --chown=app:app alembic.ini ./

USER 10001:10001
EXPOSE 8080

# Migrations first (idempotent, and serialised across replicas by a Postgres advisory lock - see
# migrations/env.py), then the server. `exec` replaces the sh wrapper so python is PID 1 and gets
# SIGTERM from Kubernetes directly: app/__main__.py drains, shuts down in order and exits 0.
ENTRYPOINT ["sh", "-c", "alembic upgrade head && exec python -m app"]
