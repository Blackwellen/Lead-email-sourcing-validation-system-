"""Prometheus metrics (process-local; the API exposes /metrics, the worker exposes its own port)."""
from prometheus_client import Counter, Gauge, Histogram

JOBS_TOTAL = Counter("lead_jobs_total", "Jobs processed", ["stage", "outcome"])
JOB_SECONDS = Histogram("lead_job_seconds", "Job duration", ["stage"], buckets=(0.05, 0.25, 1, 4, 15, 60, 240))
WORKER_ACTIVE = Gauge("lead_worker_active_jobs", "Jobs currently running in this worker")
WORKER_RSS = Gauge("lead_worker_rss_mb", "Worker resident memory MB")
QUEUE_DEPTH = Gauge("lead_queue_depth", "Queued jobs", ["stage"])
EMAILS_EXTRACTED = Counter("lead_emails_extracted_total", "Emails extracted")
PAGES_FETCHED = Counter("lead_pages_fetched_total", "Pages fetched")
DNS_LOOKUPS = Counter("lead_dns_lookups_total", "DNS lookups", ["cache"])
