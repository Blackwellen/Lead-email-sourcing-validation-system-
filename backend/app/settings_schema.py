"""Pydantic schemas + defaults for the admin-editable settings sections (stored in the Setting table)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .settings_store import get_setting, set_setting


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerSettings(_S):
    concurrency: int = Field(8, ge=1, le=512)
    poll_interval_s: float = Field(1.0, ge=0.05, le=60)
    lease_seconds: int = Field(120, ge=10, le=3600)
    max_job_attempts: int = Field(4, ge=1, le=20)
    max_rss_mb: int | None = Field(None, ge=128)


class SourcePolicySettings(_S):
    respect_robots_txt: bool = True
    default_crawl_delay_s: float = Field(1.0, ge=0, le=60)
    max_requests_per_host_per_min: int = Field(30, ge=1, le=6000)
    blocked_domains: list[str] = Field(default_factory=list, max_length=5000)
    contact_email: str = Field("", max_length=320)

    @field_validator("blocked_domains")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return sorted({d.strip().lower() for d in v if d.strip()})


MarketingPolicyValue = Literal["eligible", "needs_review", "consent_required", "not_permitted"]


class CountryMarketingPolicy(_S):
    personal: MarketingPolicyValue = "needs_review"
    role: MarketingPolicyValue = "needs_review"
    sole_trader: MarketingPolicyValue = "consent_required"
    free_provider: MarketingPolicyValue = "consent_required"
    unknown_entity: MarketingPolicyValue = "needs_review"
    allow_eligible: bool = False


class ValidationSettings(_S):
    default_mode: Literal["fast", "deep", "custom"] = "fast"
    validity_days: int = Field(30, ge=1, le=730, description="After this many days a validation counts as stale")
    recheck_after_days: int = Field(90, ge=1, le=1460)
    marketing_policy: dict[str, CountryMarketingPolicy] = Field(
        default_factory=dict, description="Per ISO country (or 'GB'/'EU'/'default' group) marketing policy; not legal advice"
    )

    @field_validator("marketing_policy")
    @classmethod
    def _keys(cls, v: dict[str, CountryMarketingPolicy]) -> dict[str, CountryMarketingPolicy]:
        for k in v:
            if k not in ("EU", "default") and not (len(k) == 2 and k.isalpha() and k.isupper()):
                raise ValueError(f"'{k}' must be a 2-letter upper-case country code, 'EU' or 'default'")
        return v


class ConcurrencySettings(_S):
    http: int = Field(16, ge=1, le=2000)
    dns: int = Field(64, ge=1, le=2000)
    per_host: int = Field(1, ge=1, le=16)
    browser_workers: int = Field(0, ge=0, le=8)
    batch_size: int = Field(250, ge=10, le=10000)


class RetentionSettings(_S):
    evidence_days: int | None = Field(None, ge=30, description="Delete contact evidence older than this (null = keep)")
    validation_history_days: int | None = Field(365, ge=7, description="Keep the latest result per check; delete older history")
    exports_days: int | None = Field(30, ge=1)
    audit_days: int | None = Field(None, ge=90)


class ExportSettings(_S):
    default_format: Literal["csv", "xlsx", "jsonl"] = "csv"
    default_preset: str = "published_business_emails"
    max_rows: int = Field(5_000_000, ge=1000)
    inline_threshold: int = Field(5000, ge=0, le=50000)


class NotificationSettings(_S):
    informational: bool = Field(True, description="These settings are stored for the operator's reference only; nothing in the worker or API sends notifications yet.")
    email_recipients: list[str] = Field(default_factory=list, max_length=50)
    notify_on_run_complete: bool = False
    notify_on_run_failure: bool = True
    webhook_url: str | None = Field(None, max_length=500)

    @field_validator("webhook_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v and not v.lower().startswith("https://"):
            raise ValueError("webhook_url must use https://")
        return v or None


class BackupSettings(_S):
    informational: bool = Field(True, description="Stored for the operator's reference only; the application does not run or verify backups (use pg_dump / your platform).")
    enabled: bool = False
    schedule_cron: str = Field("", max_length=100)
    destination_note: str = Field("", max_length=500)
    retention_days: int = Field(14, ge=1, le=3650)
    last_verified_restore_at: str | None = None


SECTIONS: dict[str, type[BaseModel]] = {
    "worker": WorkerSettings,
    "source_policies": SourcePolicySettings,
    "validation": ValidationSettings,
    "concurrency": ConcurrencySettings,
    "retention": RetentionSettings,
    "exports": ExportSettings,
    "notifications": NotificationSettings,
    "backups": BackupSettings,
}
ADVANCED = {"worker", "concurrency", "retention", "backups", "source_policies"}


async def load_section(session: AsyncSession, name: str) -> Any:
    model = SECTIONS[name]
    raw = await get_setting(session, f"settings.{name}", None)
    try:
        return model.model_validate(raw or {})
    except Exception:
        return model()


async def save_section(session: AsyncSession, name: str, value: BaseModel) -> None:
    await set_setting(session, f"settings.{name}", value.model_dump(mode="json"))
