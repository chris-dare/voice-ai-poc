# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS runtime
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    MODEL_CACHE_DIR=/app/models \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app

COPY pyproject.toml uv.lock README.md alembic.ini .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY voice_ai ./voice_ai
RUN uv sync --frozen --no-dev --no-editable

ARG WHISPER_MODEL=base
ARG WHISPER_REVISION=ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66
ENV WHISPER_MODEL=$WHISPER_MODEL \
    WHISPER_REVISION=$WHISPER_REVISION
RUN mkdir -p /app/models/kokoro && \
    WHISPER_MODEL="$WHISPER_MODEL" WHISPER_REVISION="$WHISPER_REVISION" \
      .venv/bin/python -c "import os; from faster_whisper.utils import download_model; download_model(os.environ['WHISPER_MODEL'], cache_dir='/app/models/whisper', revision=os.environ['WHISPER_REVISION'])" && \
    .venv/bin/python -c "from pathlib import Path; from voice_ai.voice.speech.assets import provision_punkt_tab; provision_punkt_tab(Path('/app/models/nltk'))" && \
    .venv/bin/python -c "from pathlib import Path; from voice_ai.voice.speech.assets import provision_kokoro; provision_kokoro(Path('/app/models/kokoro'))" && \
    chmod -R a+rX /app/models

COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
RUN useradd --system --uid 10001 --create-home voice-ai && \
    chmod 755 /app/docker/entrypoint.sh

USER 10001:10001

EXPOSE 7860 8100
ENTRYPOINT ["/app/docker/entrypoint.sh"]
