# syntax=docker/dockerfile:1

# --- base: runtime + third-party deps (cached independently of app code) ----
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
RUN groupadd --system app && useradd --system --gid app --no-create-home app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --- dev: test tooling, source arrives via bind-mount at run time ----------
FROM base AS dev
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- prod: source baked in, no reloader ------------------------------------
FROM base AS prod
COPY app ./app
USER app
EXPOSE 8000
# Single worker on purpose: call state and the dashboard hub are in-process.
# See docker-compose.yml and app/services/broadcaster.py before adding more.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20", \
     "--timeout-graceful-shutdown", "10", \
     "--no-server-header"]
