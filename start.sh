#!/bin/sh
set -eu
cd "$(dirname "$0")"
python -m finn_app.demo
exec python -m uvicorn finn_app.main:app --host "${FINN_HOST:-127.0.0.1}" --port "${PORT:-8000}"
