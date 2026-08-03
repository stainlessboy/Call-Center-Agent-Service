# --- Stage 1: build the Mini App SPA ---------------------------------------
# frontend/dist is not committed, so the bundle FastAPI serves at /app has to be
# produced here — otherwise mount_miniapp_static() finds no index.html and the
# Mini App 404s in production while working fine behind the Vite dev server.
FROM node:20-alpine AS frontend

WORKDIR /build

# Copy manifests first so `npm ci` is cached until dependencies actually change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --- Stage 2: the API ------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for asyncpg and PDF generation
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# After COPY . . so the built bundle is not shadowed by the source tree.
COPY --from=frontend /build/dist ./frontend/dist

EXPOSE 8001

CMD ["python", "main.py"]
