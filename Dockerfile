FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: gcc/libpq for psycopg2 build, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app ./app
COPY knowledge ./knowledge
COPY gunicorn_conf.py ./gunicorn_conf.py
COPY .env.example ./.env.example
COPY .env.production.example ./.env.production.example

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck hits liveness probe (fast, no DB)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).read().find(b'healthy')!=-1 else 1)" || exit 1

# Production: gunicorn + uvicorn workers (see DEPLOYMENT.md)
# Local dev override in docker-compose.yml: uvicorn --reload
CMD ["gunicorn", "app.main:app", "-c", "gunicorn_conf.py"]
