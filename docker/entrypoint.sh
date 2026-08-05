#!/bin/sh
set -eu

role="${1:-voice}"

if [ "$role" = "agent" ]; then
  exec voice-ai agent --host 0.0.0.0
fi

if [ "$role" = "migrate" ]; then
  exec voice-ai seed
fi

if [ "$role" = "worker" ]; then
  exec voice-ai worker
fi

if [ "$role" = "voice" ]; then
  exec voice-ai serve
fi

exec "$@"
