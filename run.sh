#!/usr/bin/env bash
# Launch the Content Tool Manager locally.
set -e
cd "$(dirname "$0")"
exec .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 "$@"
