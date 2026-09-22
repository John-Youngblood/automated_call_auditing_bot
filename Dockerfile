# syntax=docker/dockerfile:1

# One image: the API, plus the dashboard it serves.
#
#   gcloud run deploy call-screener --source=.
#
# Cloud Run runs a single always-on container regardless, so baking the built
# dashboard into it is free, and it removes a second host, a second deploy,
# CORS, and the build-time VITE_* URLs. See docs/deploy.md.
#
# The split images under backend/ and frontend/ are still what `make up` and
# `make office` use. This file exists for Cloud Run.

# --- dashboard: built here so no local Node is needed to deploy ------------
FROM node:24-alpine AS dashboard
WORKDIR /srv
COPY frontend/package.json frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
# Deliberately no VITE_API_BASE_URL or VITE_WS_URL: unset means same-origin,
# which is exactly right when the API is the thing serving these files.
RUN npm run build

# --- api -------------------------------------------------------------------
FROM python:3.12-slim AS prod
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
RUN groupadd --system app && useradd --system --gid app --no-create-home app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
# app/main.py mounts this at "/" when it exists, and skips it when it does
# not -- which is what keeps the dev and office stacks working unchanged.
COPY --from=dashboard /srv/dist ./app/dashboard
USER app
EXPOSE 8000
# Single worker on purpose: call state and the dashboard hub are in-process.
# `exec` matters -- without it uvicorn runs as a child of sh, which swallows
# SIGTERM and turns every deploy into a hard kill on open websockets.
# Honours $PORT, which Cloud Run injects.
CMD ["sh", "-c", "exec uvicorn app.main:app \
     --host 0.0.0.0 --port ${PORT:-8000} \
     --ws-ping-interval 20 --ws-ping-timeout 20 \
     --timeout-graceful-shutdown 10 \
     --no-server-header"]
