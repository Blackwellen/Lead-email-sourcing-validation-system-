# Architecture

Global Lead Intelligence is a self-hosted platform for discovering businesses, finding the contact addresses they publish,
collecting technical evidence about those addresses and exporting source-backed results. It is deterministic Python:
no AI service sits anywhere in the processing path.

```
 Browser ──HTTPS──► Caddy ──► Next.js dashboard (web)
                        └──► FastAPI (api) ──► PostgreSQL ◄── Python worker(s) ──► the internet
                                                  ▲                 (crawler, DNS, optional SMTP diagnostics)
                                                  └── durable job queue (FOR UPDATE SKIP LOCKED + leases)
```

| Component | Code | Role |
|---|---|---|
| API | `backend/app/main.py`, `backend/app/api/*` | Auth, RBAC, every dashboard endpoint, SSE run progress, exports |
| Worker | `backend/app/worker.py`, `backend/app/pipeline/*` | Leases jobs, runs stage handlers, heartbeats, supervises runs |
| Queue | `backend/app/jobqueue.py` | PostgreSQL table `sourcing_jobs`: idempotent enqueue, leased claims, retries, dead letters |
| Run lifecycle | `backend/app/runs.py`, `backend/app/run_config.py` | Wizard config schema, state machine, database-enforced yield counting, stop conditions |
| Sources | `backend/app/sources/*` | Plugin adapters (bulk datasets, open data, imports, search APIs, archive) |
| Discovery engines | `backend/app/discovery/*` | Cross-source resolution, business graph, lookalikes, signals, change detection, documents, completeness, gap recovery, scheduler |
| Crawler | `backend/app/crawler/*` | SSRF-safe fetcher, robots.txt, rate limits, bounded crawl frontier, extraction |
| Identity | `backend/app/pipeline/identity.py` | Organisation ↔ official-domain matching with evidence |
| Verification | `backend/app/verify/*` | V01–V12 evidence stages, scoring, marketing triage |
| Frontend | `frontend/` | Next.js App Router, TypeScript strict, Tailwind, Radix, TanStack Query/Table |

## Why PostgreSQL is the queue

The workload is network-bound and bursty. It needs durable, inspectable, restartable state more than raw queue throughput.
A table with `FOR UPDATE SKIP LOCKED` claims, leases and heartbeats gives the following:

* **Crash recovery.** A worker killed with `kill -9` leaves leased jobs. The leader-elected supervisor (`pg_try_advisory_xact_lock`)
  re-queues them when their lease expires. Jobs that exhaust their attempts go to the `dead` state and can be retried from the UI.
  This path is tested by killing a real worker process: `tests/e2e/test_restart_recovery.py`.
* **Idempotency.** `UNIQUE(run_id, stage, dedupe_key)` lets enqueueing the same work twice become a no-op.
* **Checkpoints.** Long jobs, such as streaming a 2.8 GB Companies House CSV, persist a resume cursor, so a restart never begins a million-row job from zero.
* **Pause/resume/stop.** Pausing is a run-status change; `claim` only hands out jobs of running runs.
* **Backpressure.** Discovery yields (`Requeue`) when downstream queues are saturated; per-stage semaphores bound concurrency.

Measured overhead is in `docs/TEST_REPORT.md` (no-op jobs, 4 workers). That is far above what network-bound stages need.
Redis, Celery and Kafka were not added because no test showed a need for them.

## Pipeline

Default strategy chain (`app/discovery/scheduler.py: default_strategy_pipeline`):

1. **Bulk data**: a source adapter streams `BusinessRecord`s with resumable checkpoints.
2. **Company matching**: `resolving_upsert`. Exact registration identifiers merge records; similar names never auto-merge (they go to review).
3. **Official domain discovery**: candidates come from the dataset or from name guesses. Guesses are probe candidates only and are capped at `review` unless hard evidence such as a company number in the site footer confirms them.
4. **Own-website crawl**: bounded frontier (homepage, contact, about, team, locations, relevant services, sitemap contact pages, a few PDFs).
5. Directory corroboration (database lookups), document extraction, and optional controlled permutation research (only when the run allows inferred candidates).
6. **Verification**: FAST, DEEP or CUSTOM.
7. **Qualification**: `runs.record_yield`, with `UNIQUE(run_id, unique_key)` enforcing the run's uniqueness rule.
8. **Export**.

Optional stages: network expansion, Common Crawl lookups (results stored as *historical* until a live crawl confirms them),
activity signals, change detection, lookalike expansion and monitors.

## Honesty invariants (enforced in code and tests)

* Five independent statuses per address (syntax, domain mail, recipient evidence, catch-all, overall reachability) plus a
  separate marketing status. Technical verification never implies marketing permission.
* `safe` means "relatively favourable evidence". It never means guaranteed inbox delivery. Syntax or MX alone can never produce `safe`.
* Inferred or speculative candidates are capped at a score of 55 and are never `safe` or counted as published.
* SMTP diagnostics never send `DATA`. They run only for explicitly authorised domains, and a `250` reply is recorded as
  `smtp_recipient_accepted`, never as "delivered".
* Archived (Common Crawl) contacts are labelled historical until confirmed on the live site.
* A domain is `confirmed` only with hard evidence. Every match keeps its evidence list with timestamps.
* No fabricated data: every record carries provenance (adapter, source URL, licence, parser version, observed time).
