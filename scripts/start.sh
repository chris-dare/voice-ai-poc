#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.run"
ENV_FILE="${VOICE_AI_ENV_FILE:-$ROOT_DIR/.env}"
AGENT_PID=""
GATEWAY_PID=""

cd "$ROOT_DIR"
mkdir -p "$RUN_DIR"

log() {
  printf '[voice-ai] %s\n' "$*"
}

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$GATEWAY_PID" ]] && kill -0 "$GATEWAY_PID" 2>/dev/null; then
    kill "$GATEWAY_PID" 2>/dev/null || true
  fi
  if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID" 2>/dev/null || true
  fi
  [[ -n "$GATEWAY_PID" ]] && wait "$GATEWAY_PID" 2>/dev/null || true
  [[ -n "$AGENT_PID" ]] && wait "$AGENT_PID" 2>/dev/null || true
  log 'Application services stopped. PostgreSQL remains available.'
}

wait_for_docker() {
  if docker info >/dev/null 2>&1; then
    return
  fi

  if [[ "$(uname -s)" == 'Darwin' ]]; then
    log 'Starting Docker Desktop…'
    open -a Docker
  else
    log 'Docker is not running.'
  fi

  for _attempt in {1..60}; do
    docker info >/dev/null 2>&1 && return
    sleep 2
  done

  log 'Docker did not become ready within two minutes.'
  exit 1
}

wait_for_url() {
  local name="$1"
  local url="$2"
  local pid="$3"

  for _attempt in {1..90}; do
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      log "$name is ready: $url"
      return
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      log "$name exited during startup. See $RUN_DIR/${name}.log"
      exit 1
    fi
    sleep 1
  done

  log "$name did not become ready within 90 seconds. See $RUN_DIR/${name}.log"
  exit 1
}

for command in docker uv npm curl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    log "Required command is unavailable: $command"
    exit 1
  fi
done

if [[ ! -f "$ENV_FILE" ]]; then
  log "Missing environment file: $ENV_FILE"
  log 'Copy .env.example to .env and configure it first.'
  exit 1
fi

if lsof -tiTCP:8100 -sTCP:LISTEN >/dev/null 2>&1; then
  log 'Port 8100 is already in use. Stop the existing agent before starting.'
  exit 1
fi
if lsof -tiTCP:7860 -sTCP:LISTEN >/dev/null 2>&1; then
  log 'Port 7860 is already in use. Stop the existing gateway before starting.'
  exit 1
fi

wait_for_docker

log 'Starting PostgreSQL…'
docker compose up -d postgres

log 'Building the browser client…'
npm --prefix frontend run build

log 'Applying database migrations…'
uv run --env-file "$ENV_FILE" voice-ai seed

trap cleanup EXIT INT TERM

log 'Starting agent service…'
uv run --env-file "$ENV_FILE" voice-ai agent > >(tee "$RUN_DIR/agent.log") 2>&1 &
AGENT_PID=$!
wait_for_url 'agent' 'http://127.0.0.1:8100/readyz' "$AGENT_PID"

log 'Starting voice gateway and UI…'
uv run --env-file "$ENV_FILE" voice-ai serve > >(tee "$RUN_DIR/gateway.log") 2>&1 &
GATEWAY_PID=$!
wait_for_url 'gateway' 'http://127.0.0.1:7860/readyz' "$GATEWAY_PID"

log 'Voice AI is ready at http://localhost:7860'
log 'Press Ctrl+C to stop the application services.'

while kill -0 "$AGENT_PID" 2>/dev/null && kill -0 "$GATEWAY_PID" 2>/dev/null; do
  sleep 1
done

log 'An application service exited unexpectedly. Check .run/*.log.'
exit 1
