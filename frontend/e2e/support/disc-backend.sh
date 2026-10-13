#!/usr/bin/env bash
# REAL backend for the disc-*.spec.ts suite (DB lead_fe_d, API :8104). Usage: disc-backend.sh setup|seed|start|stop
#
#   setup  create + migrate + seed lead_fe_d, create the e2e users (admin-d/operator-d/viewer-d@example.com and the biz suite's
#          admin-b/operator-b/viewer-b@example.com so biz-*.spec.ts can share this backend), then run disc_seed.py
#   seed   re-run disc_seed.py only (idempotent; SYNTHETIC example.com data)
# Then: BACKEND_URL=http://localhost:8104 npx next build && npx next start -p 3104
#       E2E_BASE_URL=http://localhost:3104 E2E_NO_SERVER=1 npx playwright test --workers=1 e2e/disc-*.spec.ts
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HERE/../../../backend"
DATA="${E2E_DATA_DIR:-/tmp/claude-0/fe_d_data}"
export LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_fe_d
export LEAD_DATA_DIR="$DATA" LEAD_EXPORT_DIR="$DATA/exports" LEAD_LOGIN_RATE_LIMIT_PER_MINUTE=1000 LEAD_RATE_LIMIT_PER_MINUTE=100000
PW='Tr0ub4dor&3-horse-staple'
case "${1:-start}" in
  setup)
    PGPASSWORD=lead psql -h localhost -U lead -d postgres -c 'create database lead_fe_d' || true
    cd "$BACKEND"
    ../.venv/bin/python -m app.cli migrate && ../.venv/bin/python -m app.cli seed
    for r in admin operator viewer; do
      for s in d b; do echo "$PW" | ../.venv/bin/python -m app.cli create-admin --email "$r-$s@example.com" --password-stdin --role "$r" || true; done
    done
    PYTHONPATH="$BACKEND" ../.venv/bin/python "$HERE/disc_seed.py";;
  seed)
    cd "$BACKEND"
    PYTHONPATH="$BACKEND" ../.venv/bin/python "$HERE/disc_seed.py";;
  start)
    mkdir -p "$DATA/exports"
    cd "$BACKEND"
    nohup ../.venv/bin/python -m uvicorn app.main:create_app --factory --port 8104 > "$DATA/api.log" 2>&1 &
    echo $! > "$DATA/api.pid"
    for _ in $(seq 1 60); do curl -sf localhost:8104/healthz >/dev/null && exit 0; sleep 0.5; done
    echo "backend did not start" >&2; tail -20 "$DATA/api.log" >&2; exit 1;;
  stop) [ -f "$DATA/api.pid" ] && kill "$(cat "$DATA/api.pid")" 2>/dev/null || true;;
esac
