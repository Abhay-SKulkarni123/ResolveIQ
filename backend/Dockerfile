# syntax=docker/dockerfile:1
#
# ResolveIQ backend image.
#
# Local development note: docker-compose.yml bind-mounts ./backend over /app and
# keeps the image's site-packages in an anonymous volume, so this image installs
# dependencies for you but your local edits are picked up by --reload.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq is needed by psycopg (binary wheel is used, but keep the client available
# for debugging); build tools are not installed because we use binary wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Dependency layer first so application edits do not invalidate the pip cache.
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "sqlalchemy>=2.0.36" \
        "alembic>=1.14" \
        "psycopg[binary]>=3.2" \
        "httpx>=0.27"

COPY app ./app
COPY scripts ./scripts
# alembic.ini is added in Phase 1 together with the first SQLAlchemy model and
# the initial migration. It is intentionally not copied yet: a migration runner
# with no models would be scaffolding for its own sake.

# Run as a non-root user. A billing service should not be root in its container.
RUN useradd --create-home --uid 1000 resolveiq \
    && chown -R resolveiq:resolveiq /app
USER resolveiq

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
