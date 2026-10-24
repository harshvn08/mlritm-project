# Build the existing React/Vite application so the FastAPI service can serve
# the frontend and API from one public origin.
FROM node:20-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source only. The .dockerignore excludes local data, .env
# files, test inputs, and developer environments from the public image.
COPY backend ./backend
COPY --from=frontend-build /src/frontend/dist ./frontend/dist

ENV APP_ENV=production \
    DEBUG=false \
    STORAGE_BACKEND=sqlite \
    ATLAS_DATA_DIR=/var/data \
    ATLAS_SQLITE_PATH=/var/data/atlas_local.sqlite3 \
    ATLAS_UPLOAD_DIR=/var/data/uploads \
    USERS_FILE_PATH=/var/data/users.json \
    BLOCKCHAIN_LEDGER_PATH=/var/data/blockchain_ledger.json

EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
