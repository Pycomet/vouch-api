# One image, several commands (M3a §2.1). compose.yml picks the command:
#   API (default):  uvicorn on port 8000
#   migrations:     alembic upgrade head   (one-shot; never run on API start)
#   timers:         python -m app.jobs.timers
#   admin setup:    python -m app.cli create-admin --phone ... --name ...
# No .env is copied in: configuration comes from the environment at run time.

FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
# The runtime dependencies only (no dev extras), into a venv the next stage copies.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install .


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

RUN groupadd --system --gid 10001 vouch \
    && useradd --system --uid 10001 --gid vouch --home-dir /app --no-create-home vouch

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# Alembic reads alembic.ini and the migration scripts from the working directory.
COPY alembic.ini ./
COPY migrations ./migrations

USER vouch
EXPOSE 8000

# Readiness (the database is reachable) through the standard library: no curl here.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

# --no-proxy-headers: X-Forwarded-For and friends are not trusted by default.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
