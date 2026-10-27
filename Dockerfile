# ============================================================
# Stage 1: Build virtual environment with uv
# ============================================================
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency specifications first for cache efficiency
COPY pyproject.toml uv.lock ./

# Install dependencies using cache mount without baking cache into image
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ============================================================
# Stage 2: Hardened, non-root runtime environment
# ============================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create unprivileged runtime user (UID 1001) per deployment-security Track A
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid appuser --shell /bin/false --no-create-home appuser

# Copy virtual environment and app code with non-root ownership
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser vapi_assistant.json ./vapi_assistant.json
COPY --chown=appuser:appuser pyproject.toml ./pyproject.toml

USER appuser

ENV PATH="/app/.venv/bin:$PATH"
ENV ENVIRONMENT=production
ENV PORT=8000
EXPOSE 8000

# Container healthcheck querying /health endpoint using standard library urllib
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]

# Single-worker invariant: In-memory asyncio.Lock booking coordinator requires --workers 1
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "10", "--log-level", "info"]
