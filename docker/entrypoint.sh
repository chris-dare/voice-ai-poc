#!/bin/sh
set -eu

role="${1:-voice}"

if [ "$role" = "agent" ]; then
  voice-ai seed --reset-demo
  exec voice-ai agent --host 0.0.0.0
fi

if [ "$role" = "voice" ]; then
  exec voice-ai serve
fi

exec "$@"
