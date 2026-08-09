# syntax=docker/dockerfile:1.7
FROM node:22-trixie-slim@sha256:db8a96a63e5264607ada2d206758876ebbed6a12be2ada7517793cbfb0c2a29c AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:5f3c58899cb4ab5b723f81641d6aed08968e6c93f9a84641321ae66ba7103f42 AS runtime
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.13 \
    PATH="/app/.venv/bin:$PATH" \
    MODEL_CACHE_DIR=/app/models \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app

COPY pyproject.toml uv.lock README.md alembic.ini .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY voice_ai ./voice_ai
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

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
# Perl is inherited from Debian as an essential package, but no production
# entrypoint or application path invokes it. Removing it from this immutable
# runtime eliminates an otherwise unused interpreter and its vulnerable core
# modules. Revisit this if runtime package management is ever introduced.
RUN apt-get remove --purge --allow-remove-essential -y perl-base && \
    rm -rf /var/lib/apt/lists/* && \
    test ! -e /usr/bin/perl && \
    .venv/bin/python -c "import av, cv2; from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport" && \
    useradd --system --uid 10001 --create-home voice-ai && \
    chmod 755 /app/docker/entrypoint.sh

USER 10001:10001

EXPOSE 7860 8100
ENTRYPOINT ["/app/docker/entrypoint.sh"]
