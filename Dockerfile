# ============================================================
# NiaBot (npk_combine_chatbot) - production image
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user. logs/ and uploads/ are written at runtime
# (utils/logger.py, domains/operations/routes.py) using paths relative to /app.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p logs uploads/tickets/attachments \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5001

# ONE worker on purpose: conversation state (core/state_manager.py) and the
# rate limiter live in process memory, so multiple workers would split a
# user's conversation across processes. Concurrency comes from threads.
# Long timeout because LLM calls + the ~15s DB pool warm-up at import.
#
# The policy RAG index (domains/policy/chroma/) is built at container start,
# not at build time: ingestion needs the OpenAI key from .env, which is only
# available at runtime. A failed ingest is logged but doesn't stop the app -
# shopping / operations keep working, only policy answers are affected.
CMD ["sh", "-c", "python -m scripts.ingest_policy || echo 'WARNING: policy ingest failed - policy answers will be unavailable'; \
     exec gunicorn app:app \
     --bind 0.0.0.0:5001 \
     --workers 1 \
     --worker-class gthread \
     --threads 8 \
     --timeout 120 \
     --access-logfile - \
     --error-logfile -"]
