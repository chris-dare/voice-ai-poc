# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    MODEL_CACHE_DIR=/app/models \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app

COPY pyproject.toml uv.lock README.md BRIEF.md AGENT_API_SPEC.md alembic.ini .python-version ./
COPY voice_ai ./voice_ai
RUN uv sync --frozen --no-dev

ARG WHISPER_MODEL=base
ARG PIPER_VOICE=en_US-lessac-medium
RUN mkdir -p /app/models/kokoro /app/models/piper && \
    .venv/bin/python -c "from faster_whisper.utils import download_model; download_model('${WHISPER_MODEL}')" && \
    .venv/bin/python -c "from pathlib import Path; from voice_ai.assets import provision_punkt_tab; provision_punkt_tab(Path('/app/models/nltk'))" && \
    .venv/bin/python -c "from pathlib import Path; from voice_ai.assets import provision_kokoro; provision_kokoro(Path('/app/models/kokoro'))" && \
    .venv/bin/python -c "from pathlib import Path; from piper.download_voices import download_voice; download_voice('${PIPER_VOICE}', Path('/app/models/piper'))"

COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN chmod 755 /app/docker/entrypoint.sh

EXPOSE 7860 8100
ENTRYPOINT ["/app/docker/entrypoint.sh"]
