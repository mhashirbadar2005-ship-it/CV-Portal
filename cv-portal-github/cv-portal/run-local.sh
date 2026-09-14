#!/usr/bin/env bash
# Local dev server with auto-reload on http://127.0.0.1:8000
set -euo pipefail
export PYTHONPATH="$(cd "$(dirname "$0")" && pwd)"
exec uvicorn app.main:app --reload --port 8000
