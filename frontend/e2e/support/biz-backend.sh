#!/usr/bin/env bash
# Starts the REAL backend for the businesses/contacts/verification/categories e2e suite (DB lead_fe_b, port 8102).
# Usage: biz-backend.sh start|stop|seed
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HERE/../../../backend"
DATA="${E2E_DATA_DIR:-/tmp/claude-0/fe_b_data}"
export LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_fe_b
export LEAD_DATA_DIR="$DATA" LEAD_EXPORT_DIR="$DATA/exports" LEAD_LOGIN_RATE_LIMIT_PER_MINUTE=1000 LEAD_RATE_LIMIT_PER_MINUTE=100000
case "${1:-start}" in
  seed)
    cd "$BACKEND"
    PYTHONPATH="$BACKEND" ../.venv/bin/python "$HERE/biz_seed.py";;
  start)
    mkdir -p "$DATA/exports"
    cd "$BACKEND"
    nohup ../.venv/bin/python -m uvicorn app.main:create_app --factory --port 8102 > "$DATA/api.log" 2>&1 &
    echo $! > "$DATA/api.pid"
    for _ in $(seq 1 40); do curl -sf localhost:8102/healthz >/dev/null && exit 0; sleep 0.5; done
    echo "backend did not start" >&2; tail -20 "$DATA/api.log" >&2; exit 1;;
  stop) [ -f "$DATA/api.pid" ] && kill "$(cat "$DATA/api.pid")" 2>/dev/null || true;;
esac
