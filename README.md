# Global Lead Intelligence

Self-hosted platform for **business discovery, published-contact extraction, email evidence collection and export**,
written in deterministic Python with a Next.js dashboard. It uses no AI services, no paid lead databases and no hidden
paid APIs. UK-first, with an international source framework.

> Log in → choose a country → choose businesses → choose sources → set a unique-contact target → choose verification
> → **Start** → monitor live → pause/resume → review evidence → **Export** (CSV / XLSX / JSONL / Listmonk).

* **Source-backed:** every company, domain match, email and signal keeps its provenance: adapter, exact URL, licence,
  parser version and timestamp.
* **Honest labels:** syntax, domain mail, recipient evidence, catch-all, reachability and marketing permission are
  independent statuses. "Safe" never means guaranteed delivery. SMTP diagnostics are off by default, never send `DATA`,
  and only run for domains you are authorised to probe. Inferred addresses are never counted as published.
* **Durable:** a PostgreSQL job queue with leases, heartbeats, checkpoints and dead letters. A run survives a worker
  being killed with `kill -9` (tested).
* **Bounded:** streaming imports and exports, keyset pagination and backpressure. The benchmarks cover 1M contact rows.

## Quick start (Docker)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD, LEAD_SECRET_KEY, SITE_ADDRESS, LEAD_CONTACT_EMAIL
docker compose up -d --build
docker compose run --rm api python -m app.cli create-admin
docker compose run --rm api python -m app.cli seed
```

Then open `https://<SITE_ADDRESS>`. See **[docs/OPERATIONS.md](docs/OPERATIONS.md)** for configuration, scaling,
backup and restore, upgrades and running without Docker.

## Documentation

| Document | Contents |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Operator guide: wizard, live runs, evidence, verification, exports, data quality |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Install, environment, workers, backups, upgrades, detected infrastructure limits |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, queue design, pipeline, honesty invariants |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | What each check proves and does not prove; SMTP policy; scoring |
| [docs/SOURCES.md](docs/SOURCES.md) | Every source adapter: coverage, licence, credentials, live-verification status |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | Test suites, live acceptance runs, benchmarks (measured), known limitations |

## Repository layout

```
backend/            FastAPI API, worker, pipeline, sources, discovery engines, crawler, verification (Python 3.12+)
  app/              application code            alembic/   migrations
  tests/            pytest suites (unit, API, integration, e2e, SMTP, migrations)
  bench/            measured benchmarks (python -m bench.run_bench)
frontend/           Next.js App Router dashboard (TypeScript strict, Tailwind, Radix, TanStack)
  e2e/              Playwright suites
deploy/             Caddyfile (HTTPS reverse proxy), backup script
docker-compose.yml  db, migrate, api, worker, web, proxy, backup
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e "backend[dev]"
cd backend && LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead_test ../.venv/bin/pytest -m "not live"
cd frontend && npm ci && npm run typecheck && npm run lint && npm run build
```
