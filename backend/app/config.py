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
    max_json_body_kb: int = 1024  # non-multipart request bodies (JSON) are refused above this size before parsing
    rate_limit_per_minute: int = 240
    login_rate_limit_per_minute: int = 10
    # Reverse proxy: when true, the client address is taken from X-Forwarded-For (right-most hop that is not a trusted
    # proxy). trusted_proxies: comma separated CIDRs; empty = loopback + private ranges (the docker networks).
    trust_proxy: bool = False
    trusted_proxies: str = ""
    # live run views (SSE)
    sse_max_streams_per_user: int = 3
    sse_max_streams: int = 100

    # outbound fetching
    user_agent: str = "LeadIntelBot/0.1 (+contact: set LEAD_CONTACT_EMAIL)"
    contact_email: str = ""
    allow_private_networks: bool = False  # ONLY for tests against local fixture servers
    http_max_bytes: int = 2_000_000
    http_timeout_s: float = 15.0
    # CA bundle for outbound TLS (e.g. a corporate TLS-inspecting proxy). Empty = use $SSL_CERT_FILE if set, else certifi.
    ca_bundle: str = ""

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


DEFAULT_SECRET_PREFIX = "change-me"


def is_default_secret(key: str) -> bool:
    return not key or key.startswith(DEFAULT_SECRET_PREFIX)


def ensure_secret_key(settings: Settings) -> Settings:
    """Never run with the shipped default secret key.

    * production: refuse (``RuntimeError``) -- also when it is shorter than 32 characters;
    * test: allowed (deterministic fixtures);
    * anything else (development ...): a random key is generated once, persisted (mode 0600) in
      ``data_dir/.secret_key`` and used from then on, with a warning.
    """
    if settings.env == "production":
        if is_default_secret(settings.secret_key) or len(settings.secret_key) < 32:
            raise RuntimeError("LEAD_SECRET_KEY must be set to a unique value of at least 32 characters in production")
        return settings
    if settings.env == "test" or not is_default_secret(settings.secret_key):
        return settings
    import logging
    import os
    import secrets
    from pathlib import Path

    path = Path(settings.data_dir) / ".secret_key"
    key = ""
    try:
        key = path.read_text().strip()
    except OSError:
        pass
    if len(key) < 32:
        key = secrets.token_urlsafe(48)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key)
        except OSError as e:
            logging.getLogger("config").warning("could not persist the generated secret key (%s); sessions and sealed secrets will not survive a restart", e)
    logging.getLogger("config").warning("LEAD_SECRET_KEY is not set: using a generated key stored in %s (set LEAD_SECRET_KEY explicitly in any shared deployment)", path)
    return settings.model_copy(update={"secret_key": key})


@lru_cache
def get_settings() -> Settings:
    return ensure_secret_key(Settings())
