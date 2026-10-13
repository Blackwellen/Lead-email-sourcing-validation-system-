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

    @model_validator(mode="after")
    def _check(self):
        self.countries = [c.upper() for c in self.countries]
        if not self.sources and not self.imported_domains:
            raise ValueError("choose at least one source or provide imported domains")
        return self
