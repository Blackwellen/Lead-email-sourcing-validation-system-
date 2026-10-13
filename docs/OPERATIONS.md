# Operations guide

## 1. Install with Docker Compose (VPS, Oracle Cloud, local Linux)

Requirements: Docker Engine 24+ with Compose v2, 2 vCPU / 4 GB RAM minimum (4 GB+ recommended for 1M+ contacts),
20 GB disk, plus about 3 GB per Companies House snapshot if you use it.

```bash
git clone <repo> lead-intel && cd lead-intel
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD, LEAD_SECRET_KEY (openssl rand -hex 32), SITE_ADDRESS, LEAD_CONTACT_EMAIL
docker compose up -d --build          # db -> migrate (one-shot) -> api, worker, web, proxy, backup
docker compose run --rm api python -m app.cli create-admin      # prompts for email + password (12+ chars)
docker compose run --rm api python -m app.cli seed              # built-in taxonomy + source adapter registry
```

Open `https://SITE_ADDRESS`. Caddy obtains a Let's Encrypt certificate automatically when `SITE_ADDRESS` is a public DNS
name that points at the host (ports 80 and 443 must be open). With `localhost` it uses a local self-signed certificate.

### Services

| Service | Image | Restart | Health |
|---|---|---|---|
| `db` | postgres:16-alpine | unless-stopped | `pg_isready` |
| `migrate` | backend | runs once (`alembic upgrade head`) | exit 0 gates api/worker |
| `api` | backend (uvicorn) | unless-stopped | `GET /healthz` |
| `worker` | backend (`python -m app.worker`) | always, 40 s stop grace | heartbeats in `worker_heartbeats` (System page) |
| `web` | Next.js standalone | unless-stopped | `GET /` |
| `proxy` | caddy:2 | unless-stopped | HTTPS, HSTS, SSE flushing, 60 MB body cap |
| `backup` | postgres:16-alpine | unless-stopped | daily `pg_dump -Fc`, rotated after `BACKUP_KEEP_DAYS` |

Networks: `db`, `migrate`, `web` and `backup` sit only on the internal network. `api` and `worker` also have egress,
because the worker crawls the web. `/metrics` (Prometheus) is never routed by Caddy; scrape it from inside the network.

### Scaling workers

Workers are stateless. Run more of them with `docker compose up -d --scale worker=3`.

| Variable | Meaning |
|---|---|
| `LEAD_WORKER_CONCURRENCY` | concurrent jobs per worker process |
| `LEAD_WORKER_MAX_RSS_MB` | worker stops claiming new work above this memory use (backpressure) |
| `LEAD_WORKER_STAGES` | comma list to dedicate a worker to some stages, e.g. `verify,export` |

## 2. Configuration reference (environment, prefix `LEAD_`)

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local dev URL | `postgresql+asyncpg://…` |
| `SECRET_KEY` | — | **Required in production.** Signs sessions and encrypts TOTP and source secrets. Never change it casually: sealed secrets become unreadable. Outside production a random key is generated and stored in `DATA_DIR/.secret_key`. |
| `ENV` | development | `production` enables secure cookies (required), hides `/api/docs` and trims `/readyz` |
| `COOKIE_SECURE` | false | must be true in production (behind HTTPS) |
| `TRUST_PROXY` / `TRUSTED_PROXIES` | false / private ranges | real client IP from `X-Forwarded-For` (right-most untrusted hop); compose sets this for Caddy |
| `CONTACT_EMAIL` | — | appended to the crawler User-Agent so site owners can reach you |
| `CA_BUNDLE` | — | CA file for outbound TLS behind an inspecting proxy (the standard `SSL_CERT_FILE` also works) |
| `MAX_UPLOAD_MB` | 50 | upload limit; JSON bodies are capped separately at ~1 MB |
| `RATE_LIMIT_PER_MINUTE` / `LOGIN_RATE_LIMIT_PER_MINUTE` | 240 / 10 | per client IP; login also per (IP, account) |
| `SMTP_DIAGNOSTICS_ENABLED`, `SMTP_HELO_HOST`, `SMTP_MAIL_FROM`, `SMTP_AUTHORISED_DOMAINS` | off | see `docs/VERIFICATION.md` |
| `WORKER_LEASE_SECONDS` / `WORKER_HEARTBEAT_SECONDS` / `MAX_JOB_ATTEMPTS` | 120 / 15 / 4 | queue tuning |

Everything else (worker presets, validation defaults, marketing policy per country, retention, export defaults) is
edited by admins in **Settings**, stored in the database and audited.

## 3. Day-2 operations

* **Health.** Use the System page (workers, queue, DNS resolver self-test, port-25 egress check, CPU/RAM/disk) or
  `GET /readyz`. Prometheus metrics are at `api:8000/metrics` (internal only).
* **Pause everything.** System → *Pause all runs* is safe: in-flight jobs finish and commit, and nothing new is claimed.
  *Resume processing* resumes exactly the runs it paused.
* **Maintenance mode.** Writes return 503 with a message and workers stop claiming jobs. Use it for upgrades.
* **Failed work.** System → *Failed jobs* or Run → *Retry failed*. Jobs that exhaust their retries become `dead`
  (dead-letter) and stay until retried.
* **Data retention.** Settings → Data retention, then *Run now*, or `python -m app.cli apply-retention`.
* **Right to object / erasure.** Contacts → *Suppress* or the privacy endpoint `POST /api/privacy/objection`. Suppression
  entries persist so a deleted address is never collected again.

## 4. Backups and restore

The `backup` service writes `/backups/lead-<UTC timestamp>.dump` daily (custom format), keeps files for `BACKUP_KEEP_DAYS`
days and logs `backup ok` or `backup FAILED`. Copy the `backups` volume off the host; an on-host backup is not a backup.

To restore into a fresh stack:

```bash
docker compose stop api worker web
docker compose exec db dropdb -U lead lead && docker compose exec db createdb -U lead lead
docker compose cp ./lead-YYYYMMDDTHHMMSSZ.dump db:/tmp/restore.dump
docker compose exec db pg_restore -U lead -d lead --no-owner /tmp/restore.dump
docker compose run --rm migrate          # no-op if the dump is at head; upgrades an older dump
docker compose start api worker web
```

Also back up `.env`, especially `LEAD_SECRET_KEY`. Without it, sealed source secrets and TOTP enrolments cannot be decrypted.

## 5. Upgrades and rollback

```bash
git pull
docker compose build
# optional: System → Maintenance mode ON (or pause all runs)
docker compose up -d                   # migrate runs first; api/worker start only if it succeeds
```

Workers stopping during the upgrade is safe: leased jobs are recovered when their lease expires, and checkpoints resume
long jobs. To roll back, check out the previous tag, run `docker compose run --rm api alembic downgrade <previous
revision>` **only if** the newer release added a migration (see `backend/alembic/versions`), then `docker compose up -d --build`.
Take a backup before any migration.

## 6. Running without Docker (development)

```bash
# PostgreSQL 16 running locally
python3 -m venv .venv && .venv/bin/pip install -e "backend[dev]"
cd backend
export LEAD_DATABASE_URL=postgresql+asyncpg://lead:lead@localhost:5432/lead
../.venv/bin/python -m app.cli migrate && ../.venv/bin/python -m app.cli seed && ../.venv/bin/python -m app.cli create-admin
../.venv/bin/uvicorn app.main:create_app --factory --port 8000 &
../.venv/bin/python -m app.worker &
cd ../frontend && npm ci && BACKEND_URL=http://localhost:8000 npm run build
cp -r .next/static .next/standalone/.next/ && cp -r public .next/standalone/ 2>/dev/null; PORT=3000 node .next/standalone/server.js
```

`BACKEND_URL` is read when the frontend is **built** (Next.js bakes rewrites into the standalone build). Rebuild the
frontend if the API address changes.

## 7. Infrastructure limits the platform detects and reports

| Limit | Where it shows | Effect |
|---|---|---|
| Outbound port 25 blocked (most clouds) | System → SMTP diagnostics → *Check outbound port 25* | SMTP diagnostics stay `untested`; nothing is guessed |
| DNS resolver answers A but not MX | System → DNS → *MX self-test* | Implicit-MX results are reported `unknown`, not `accepts_mail` |
| TLS-inspecting proxy | crawl errors `tls_failure` | set `LEAD_CA_BUNDLE`; verification is never disabled |
| No Playwright / Chromium | System → Browser workers | JS-only sites are not rendered (most business sites don't need it) |
| Worker memory ceiling | worker heartbeat RSS, backpressure | worker pauses claiming until memory drops |
