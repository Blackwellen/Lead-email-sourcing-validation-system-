#!/usr/bin/env bash
# Starts the REAL backend for the ops e2e suite (DB lead_fe_c, port 8103). Usage: ops-backend.sh start|stop
#
# One-time setup (synthetic data only):
#   PGPASSWORD=lead psql -h localhost -U lead -d postgres -c 'create database lead_fe_c'
#   cd backend && export LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_fe_c
#   ../.venv/bin/python -m app.cli migrate && ../.venv/bin/python -m app.cli seed
#   echo 'Adm1n-Passw0rd-xyz!Q'     | ../.venv/bin/python -m app.cli create-admin --email admin@e2e.test    --password-stdin --role admin
#   echo 'Op3rator-Passw0rd-xyz!Q'  | ../.venv/bin/python -m app.cli create-admin --email operator@e2e.test --password-stdin --role operator
#   echo 'V1ewer-Passw0rd-xyz!Q'    | ../.venv/bin/python -m app.cli create-admin --email viewer@e2e.test   --password-stdin --role viewer
#   PGPASSWORD=lead psql -h localhost -U lead -d lead_fe_c -f frontend/e2e/support/ops-seed.sql
# Then: ops-backend.sh start; BACKEND_URL=http://localhost:8103 npx next dev -p 3103 (or build + start);
#   E2E_BASE_URL=http://localhost:3103 E2E_NO_SERVER=1 npx playwright test --workers=1 e2e/ops-*.spec.ts
# The specs skip themselves when the API on :8103 is not reachable.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HERE/../../../backend"
DATA="${E2E_DATA_DIR:-/tmp/claude-0/fe_c_data}"
export LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_fe_c
export LEAD_DATA_DIR="$DATA" LEAD_EXPORT_DIR="$DATA/exports" LEAD_LOGIN_RATE_LIMIT_PER_MINUTE=1000 LEAD_RATE_LIMIT_PER_MINUTE=100000
case "${1:-start}" in
  start)
    mkdir -p "$DATA/exports"
    cd "$BACKEND"
    nohup ../.venv/bin/python -m uvicorn app.main:create_app --factory --port 8103 > "$DATA/api.log" 2>&1 &
    echo $! > "$DATA/api.pid"
    for _ in $(seq 1 40); do curl -sf localhost:8103/healthz >/dev/null && exit 0; sleep 0.5; done
    echo "backend did not start" >&2; tail -20 "$DATA/api.log" >&2; exit 1;;
  stop) [ -f "$DATA/api.pid" ] && kill "$(cat "$DATA/api.pid")" 2>/dev/null || true;;
esac
