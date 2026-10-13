"""Pydantic schema for a sourcing run's configuration snapshot (wizard output)."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TargetDefinition(StrEnum):
    distinct_companies = "distinct_companies"
    companies_with_domains = "companies_with_domains"
    distinct_published_emails = "distinct_published_emails"
    distinct_email_domains = "distinct_email_domains"
    business_email_pairs = "business_email_pairs"
    validated_contacts = "validated_contacts"  # confidence >= min_confidence
    campaign_eligible_emails = "campaign_eligible_emails"


class Uniqueness(StrEnum):
    email = "email"  # unique emails globally
    organisation = "organisation"  # one email per business
    domain = "domain"  # one email per registrable domain


class TargetSpec(BaseModel):
    definition: TargetDefinition = TargetDefinition.distinct_published_emails
    count: int = Field(100, ge=1, le=50_000_000)
    uniqueness: Uniqueness = Uniqueness.email
    max_contacts_per_company: int = Field(5, ge=1, le=1000)
    contact_type: Literal["named", "role", "both"] = "both"
    published_only: bool = True
    allow_inferred: bool = False
    min_confidence: int = Field(40, ge=0, le=100)
    exclude_free_providers: bool = True
    exclude_catchall_or_inconclusive: bool = False
    exclude_previous_bounces: bool = True
    exclude_suppressed: bool = True
    max_runtime_minutes: int | None = Field(None, ge=1)
    max_work_jobs: int | None = Field(None, ge=1)


class VerificationSpec(BaseModel):
    mode: Literal["fast", "deep", "custom"] = "fast"
    custom: dict = Field(default_factory=dict)  # VerifyOptions fields when mode == custom


class PerformanceSpec(BaseModel):
    preset: Literal["conservative", "balanced", "advanced"] = "conservative"
    batch_size: Literal[250, 500, 1000, 2500, 5000, 10000] = 250
    http_concurrency: int | None = Field(None, ge=1, le=2000)
    per_host_concurrency: int = Field(1, ge=1, le=16)
    dns_concurrency: int | None = Field(None, ge=1, le=2000)
    worker_limit: int | None = Field(None, ge=1, le=512)
    browser_workers: int = Field(0, ge=0, le=8)
    max_ram_mb: int | None = Field(None, ge=128)
    desired_websites_per_hour: int | None = Field(None, ge=1)
    desired_businesses_per_hour: int | None = Field(None, ge=1)
    desired_records_per_hour: int | None = Field(None, ge=1)
    max_requests_per_host_per_min: int = Field(30, ge=1)
    max_daily_operations: int | None = Field(None, ge=1)
    retry_max_attempts: int = Field(4, ge=1, le=10)
    resume_existing: bool = True


SOURCE_CATEGORIES = (
    "bulk_open_data", "industry_registers", "licensed_directories", "procurement",
    "company_websites", "common_crawl", "documents", "network_expansion",
)


class NetworkSpec(BaseModel):
    """DISC-06 business-network expansion controls."""

    enabled: bool = False
    max_depth: int = Field(1, ge=1, le=3)
    source_budget: int = Field(50, ge=1, le=100_000)  # max page fetches per seed organisation


class SignalsSpec(BaseModel):
    """DISC-09 business-signal detection controls (empty ``types`` = all types)."""

    enabled: bool = False
    types: list[str] = Field(default_factory=list)
    freshness_days: int = Field(180, ge=1, le=3650)


class DiscoverySpec(BaseModel):
    """Operator controls for the discovery-intelligence engines and the smart source scheduler.

    Every field has a conservative default so existing run snapshots (which lack ``discovery``) keep working.
    """

    strategy: Literal["fast", "balanced", "deep", "custom"] = "balanced"
    source_categories: list[Literal[
        "bulk_open_data", "industry_registers", "licensed_directories", "procurement",
        "company_websites", "common_crawl", "documents", "network_expansion",
    ]] = Field(default_factory=lambda: ["bulk_open_data", "industry_registers", "company_websites"])
    contact_discovery: Literal["published_only", "published_plus_corroborated", "include_inferred"] = "published_only"
    business_scope: Literal["new_only", "refresh_existing", "both"] = "new_only"
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    signals: SignalsSpec = Field(default_factory=SignalsSpec)
    source_quotas: dict[str, int] = Field(default_factory=dict)  # adapter id or source category -> max work units
    overall_max_work: int | None = Field(None, ge=1)
    fallback_research: Literal["stop_if_no_website", "try_other_sources", "flag_for_review"] = "stop_if_no_website"
    search_budget: int = Field(0, ge=0, le=50)  # search-provider queries allowed per company (0 = none)

    @model_validator(mode="after")
    def _check(self):
        for k, v in self.source_quotas.items():
            if v < 0:
                raise ValueError(f"source quota for {k!r} must be >= 0")
        self.source_categories = list(dict.fromkeys(self.source_categories))
        return self


class RunConfig(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    destination_category_id: int | None = None
    countries: list[str] = Field(default_factory=lambda: ["GB"], min_length=1)
    regions: list[str] = Field(default_factory=list)
    localities: list[str] = Field(default_factory=list)
    postcodes: list[str] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    active_only: bool = True
    refresh_existing: bool = False
    sources: list[str] = Field(default_factory=list)  # adapter ids in priority order
    source_params: dict[str, dict] = Field(default_factory=dict)  # per-adapter params (file paths, urls)
    imported_domains: list[str] = Field(default_factory=list)
    target: TargetSpec = Field(default_factory=TargetSpec)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    performance: PerformanceSpec = Field(default_factory=PerformanceSpec)
    schedule_at: str | None = None  # ISO timestamp
    crawl_max_pages: int = Field(12, ge=1, le=100)
    discovery: DiscoverySpec = Field(default_factory=DiscoverySpec)

    @model_validator(mode="after")
    def _check(self):
        self.countries = [c.upper() for c in self.countries]
        if not self.sources and not self.imported_domains:
            raise ValueError("choose at least one source or provide imported domains")
        return self
