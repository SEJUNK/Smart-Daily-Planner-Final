# === Dockerfile ===
# Multi-stage build for Smart Daily Planner
# Stage 1: Builder — install dependencies
# Stage 2: Runtime — non-root user, minimal image

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .

# Install into a prefix directory for easy copying
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create non-root user
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=appuser:appgroup . .

# Create empty __init__.py files for package discovery
RUN touch config/__init__.py tools/__init__.py agents/__init__.py \
         mcp_servers/__init__.py api/__init__.py

# Switch to non-root user
USER appuser

# Cloud Run injects PORT as an environment variable
ENV PORT=8080
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose the application port
EXPOSE $PORT

# Health check for Cloud Run
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen(f'http://localhost:{__import__(\"os\").environ.get(\"PORT\",8080)}/health')" || exit 1

# Start the FastAPI server using uvicorn
# Cloud Run expects the app to listen on $PORT
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info"]
