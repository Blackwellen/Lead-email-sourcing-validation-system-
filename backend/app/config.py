"""Application settings (environment-driven)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEAD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lead:lead@localhost:5432/lead"
    secret_key: str = "change-me-in-production-please-use-32+chars"
    env: str = "development"  # development | production | test
    cookie_secure: bool = False
    session_ttl_hours: int = 12
    cors_origins: str = "http://localhost:3000"
    data_dir: str = "./var"  # downloads, exports, uploads
    export_dir: str = "./var/exports"
    max_upload_mb: int = 50
    rate_limit_per_minute: int = 240
    login_rate_limit_per_minute: int = 10

    # outbound fetching
    user_agent: str = "LeadIntelBot/0.1 (+contact: set LEAD_CONTACT_EMAIL)"
    contact_email: str = ""
    allow_private_networks: bool = False  # ONLY for tests against local fixture servers
    http_max_bytes: int = 2_000_000
    http_timeout_s: float = 15.0

    # SMTP diagnostics -- disabled unless explicitly configured
    smtp_diagnostics_enabled: bool = False
    smtp_helo_host: str = ""
    smtp_mail_from: str = ""  # must be an address on a domain you own
    smtp_authorised_domains: str = ""  # comma separated; probes ONLY allowed for these (or per-domain permission rows)

    # worker
    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: int = 15
    worker_concurrency: int = 8
    worker_poll_interval_s: float = 1.0
    max_job_attempts: int = 4

    initial_admin_email: str = ""
    initial_admin_password: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
