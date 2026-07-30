# ── Stage 1: build del frontend React ────────────────────────────────────────
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

# Solo copiar package files primero para aprovechar cache de capas
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build


# ── Stage 2: imagen Python con el bot + API ───────────────────────────────────
FROM python:3.11-slim

# Dependencias del sistema necesarias para psycopg2 y ccxt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    --index-url https://pypi.org/simple/

# Código del proyecto
COPY . .

# Build del frontend copiado desde el stage anterior
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Directorio de logs persistible como volumen
RUN mkdir -p logs

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
