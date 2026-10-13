"""Relational model. Everything durable lives in PostgreSQL (including the job queue)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now():
    return func.now()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), onupdate=_now())


# --------------------------------------------------------------------------- security / admin
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="operator", server_default=sa_text("'operator'"))  # admin | operator | viewer
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("role in ('admin','operator','viewer')", name="ck_user_role"),)


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80), index=True)
    object_type: Mapped[str | None] = mapped_column(String(40))
    object_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), onupdate=_now())


# --------------------------------------------------------------------------- taxonomy
class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    level: Mapped[str] = mapped_column(String(16), default="category", server_default=sa_text("'category'"))  # category|subcategory|specialism
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    synonyms: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    excluded_keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    sic_codes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    osm_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))  # e.g. "shop=estate_agent"
    default_sources: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'))
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))


# --------------------------------------------------------------------------- core entities
class Organisation(Base, TimestampMixin):
    __tablename__ = "organisations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(String(400))
    trading_name: Mapped[str | None] = mapped_column(String(400))
    company_number: Mapped[str | None] = mapped_column(String(40))
    legal_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(60))
    country_code: Mapped[str] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(60))
    industry: Mapped[str | None] = mapped_column(String(200))
    subcategory: Mapped[str | None] = mapped_column(String(200))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    sic_codes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canonical_identity: Mapped[str] = mapped_column(String(500))
    discovery_source: Mapped[str | None] = mapped_column(String(80))
    source_ref: Mapped[str | None] = mapped_column(String(300))  # id in the source dataset
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="SET NULL"))

    domains: Mapped[list[OrganisationDomain]] = relationship(back_populates="organisation")

    __table_args__ = (
        UniqueConstraint("canonical_identity", name="uq_org_canonical_identity"),
        Index("ix_org_company_number", "country_code", "company_number", postgresql_where=text("company_number is not null")),
        Index("ix_org_country_region_city", "country_code", "region", "city"),
        Index("ix_org_name_trgm_prefix", text("lower(legal_name) text_pattern_ops")),
        Index("ix_org_trading_prefix", text("lower(trading_name) text_pattern_ops")),  # DISC-12 resolution lookups
        Index("ix_org_country_postcode", "country_code", "postal_code"),
        Index("ix_org_sic_gin", "sic_codes", postgresql_using="gin"),  # DISC-07 lookalike pre-filter
    )


class OrganisationDomain(Base, TimestampMixin):
    __tablename__ = "organisation_domains"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    registrable_domain: Mapped[str] = mapped_column(String(255), index=True)
    website_url: Mapped[str | None] = mapped_column(Text)
    # confirmed | high | review | mismatch | unknown
    verified_match_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default=sa_text("'unknown'"))
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text('0.0'))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    parked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    organisation: Mapped[Organisation] = relationship(back_populates="domains")
    __table_args__ = (UniqueConstraint("organisation_id", "domain", name="uq_orgdomain"),)


class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str | None] = mapped_column(String(300))
    given_name: Mapped[str | None] = mapped_column(String(150))
    family_name: Mapped[str | None] = mapped_column(String(150))
    job_title: Mapped[str | None] = mapped_column(String(300))
    department: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(2))
    identity_confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text('0.0'))
    identity_key: Mapped[str | None] = mapped_column(String(400))
    __table_args__ = (
        UniqueConstraint("organisation_id", "identity_key", name="uq_contact_org_identity"),
    )


class EmailAddress(Base, TimestampMixin):
    __tablename__ = "email_addresses"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    email_original: Mapped[str] = mapped_column(String(320))
    email_normalised: Mapped[str] = mapped_column(String(320))
    local_part: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    registrable_domain: Mapped[str] = mapped_column(String(255), index=True)
    source_type: Mapped[str] = mapped_column(String(40))  # first observation source type
    # published | corroborated | pattern_inferred | role_speculative
    origin: Mapped[str] = mapped_column(String(24), default="published", server_default=sa_text("'published'"))
    role_type: Mapped[str | None] = mapped_column(String(40))
    is_free_provider: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    is_disposable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text('0.0'))
    confidence_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    # independent statuses (spec section 10)
    syntax_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default=sa_text("'unknown'"))
    domain_mail_status: Mapped[str] = mapped_column(String(24), default="unknown", server_default=sa_text("'unknown'"))
    recipient_evidence_status: Mapped[str] = mapped_column(String(32), default="untested", server_default=sa_text("'untested'"))
    catch_all_status: Mapped[str] = mapped_column(String(32), default="untested", server_default=sa_text("'untested'"))
    overall_reachability: Mapped[str] = mapped_column(String(16), default="unknown", server_default=sa_text("'unknown'"))
    marketing_status: Mapped[str] = mapped_column(String(24), default="needs_review", server_default=sa_text("'needs_review'"))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    previously_bounced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("email_normalised", name="uq_email_normalised"),
        Index("ix_email_org", "organisation_id", "origin"),
        Index("ix_email_reach", "overall_reachability", "origin"),
        Index("ix_email_lastval", "last_validated_at"),
        Index("ix_email_keyset", "id"),
        CheckConstraint("origin in ('published','corroborated','pattern_inferred','role_speculative')", name="ck_email_origin"),
    )


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_adapter: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_document: Mapped[str | None] = mapped_column(String(300))
    licence: Mapped[str | None] = mapped_column(String(200))
    evidence_type: Mapped[str] = mapped_column(String(40))  # mailto|text|jsonld|vcard|pdf|obfuscated|dataset|manual|permutation
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    parser_version: Mapped[str] = mapped_column(String(40), default="1", server_default=sa_text("'1'"))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    source_reliability: Mapped[float] = mapped_column(Float, default=0.5, server_default=sa_text('0.5'))
    historical: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))  # e.g. Common Crawl archive, unconfirmed against live site
    archive_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("source_adapter", "source_url", "evidence_type", "content_hash", name="uq_evidence_identity"),
    )


class ContactEvidence(Base):
    __tablename__ = "contact_evidence"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("email_addresses.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    organisation_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence_sources.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="SET NULL"), index=True)
    extraction_method: Mapped[str | None] = mapped_column(String(40))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))  # name/title/phone observed alongside
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("email_id", "evidence_id", "extraction_method", name="uq_contact_evidence"),)


class ValidationResult(Base):
    __tablename__ = "validation_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("email_addresses.id", ondelete="CASCADE"))
    check_type: Mapped[str] = mapped_column(String(32))  # V01_syntax, V02_domain, V03_mx, V05_disposable ...
    result: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    response_code: Mapped[int | None] = mapped_column(Integer)
    enhanced_status_code: Mapped[str | None] = mapped_column(String(12))
    provider: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verifier_version: Mapped[str] = mapped_column(String(20), default="1", server_default=sa_text("'1'"))
    permission_context: Mapped[str | None] = mapped_column(String(60))
    __table_args__ = (Index("ix_valres_email_time", "email_id", "observed_at"),)


class DomainCache(Base):
    """Per-domain DNS / MX / provider knowledge, shared by every email on that domain."""

    __tablename__ = "domain_cache"
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    mail_status: Mapped[str] = mapped_column(String(24))  # accepts_mail|no_mail_service|null_mx|temporary_dns_failure|unknown
    mx: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    provider: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DomainPermission(Base):
    """Explicit authorisation to run SMTP recipient diagnostics against a domain."""

    __tablename__ = "domain_permissions"
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    smtp_probe_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    basis: Mapped[str] = mapped_column(String(300))  # why authorised (owned domain, written consent...)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class DeliveryEvent(Base):
    """V10: feedback imported from a real sending platform."""

    __tablename__ = "delivery_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("email_addresses.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(24))  # hard_bounce|soft_bounce|accepted|complaint|unsubscribe|deferral
    campaign: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("email_id", "event_type", "campaign", "occurred_at", name="uq_delivery_event"),)


class SuppressionEntry(Base):
    __tablename__ = "suppression_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))  # email | domain | company
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(40))  # opt_out|objection|bounce|complaint|manual|legal
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression"),)


# --------------------------------------------------------------------------- runs / queue
class SourcingRun(Base):
    __tablename__ = "sourcing_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    destination_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    target_count: Mapped[int] = mapped_column(Integer, default=100, server_default=sa_text('100'))
    target_definition: Mapped[str] = mapped_column(String(40), default="distinct_published_emails", server_default=sa_text("'distinct_published_emails'"))
    uniqueness: Mapped[str] = mapped_column(String(24), default="email", server_default=sa_text("'email'"))  # email | organisation | domain
    # draft|scheduled|queued|running|pausing|paused|stopping|completed|failed|cancelled|archived
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default=sa_text("'draft'"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(60))
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))


class RunYield(Base):
    """Database-enforced uniqueness for stop-condition counting.

    ``unique_key`` is the email, organisation id or registrable domain depending on the run's
    uniqueness rule, so UNIQUE(run_id, unique_key) makes double counting impossible.
    """

    __tablename__ = "run_yield"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="CASCADE"))
    unique_key: Mapped[str] = mapped_column(String(400))
    email_id: Mapped[int] = mapped_column(ForeignKey("email_addresses.id", ondelete="CASCADE"))
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"))
    qualified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text('false'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (
        UniqueConstraint("run_id", "unique_key", name="uq_runyield_key"),
        Index("ix_runyield_run_email", "run_id", "email_id"),
    )


class RunOrganisation(Base):
    __tablename__ = "run_organisations"
    run_id: Mapped[int] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="CASCADE"), primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(24), default="discovered", server_default=sa_text("'discovered'"))


class SourcingJob(Base):
    __tablename__ = "sourcing_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(24))  # import|discover|resolve_domain|crawl|verify|revalidate|export
    dedupe_key: Mapped[str] = mapped_column(String(400))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    # queued|leased|done|failed|dead|cancelled|paused
    state: Mapped[str] = mapped_column(String(12), default="queued", server_default=sa_text("'queued'"))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=sa_text('100'))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    worker_id: Mapped[str | None] = mapped_column(String(80))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    host_key: Mapped[str | None] = mapped_column(String(255))  # for per-host throttling
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("run_id", "stage", "dedupe_key", name="uq_job_idem"),
        Index("ix_job_claim", "state", "priority", "next_attempt_at"),
        Index("ix_job_run_state", "run_id", "state", "stage"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    hostname: Mapped[str | None] = mapped_column(String(120))
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    rss_mb: Mapped[float | None] = mapped_column(Float)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))


class RunMetric(Base):
    __tablename__ = "run_metrics"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="CASCADE"), index=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint("run_id", "bucket", name="uq_runmetric_bucket"),)


class CrawlCache(Base):
    """Conditional-request validators + last result per URL (incremental crawling)."""

    __tablename__ = "crawl_cache"
    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceAdapterState(Base):
    __tablename__ = "source_adapter_state"
    adapter_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text('true'))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default=sa_text('100'))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runs_ok: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    runs_failed: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    businesses_recovered: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    last_error: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    preset: Mapped[str] = mapped_column(String(40))
    format: Mapped[str] = mapped_column(String(8))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(12), default="queued", server_default=sa_text("'queued'"))  # queued|running|done|failed
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    file_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="SET NULL"))


class Preset(Base, TimestampMixin):
    """Saved run configuration / search preset / saved table view."""

    __tablename__ = "presets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # run | view_contacts | view_businesses
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_preset_name"),)


class VerificationBatch(Base):
    __tablename__ = "verification_batches"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    name: Mapped[str] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(10), default="fast", server_default=sa_text("'fast'"))
    status: Mapped[str] = mapped_column(String(12), default="queued", server_default=sa_text("'queued'"))  # queued|running|paused|done|cancelled
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    done: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text('0'))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="SET NULL"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))


# --------------------------------------------------------------------------- source provenance (sources module)
class OrganisationSource(Base):
    """Per-record provenance: which adapter/dataset produced which facts, under which licence.

    One row per (organisation, adapter, source_ref). ``email_published`` is only ever set when the
    source dataset itself publishes the address (e.g. an OSM ``email`` tag); the ingest stage may turn
    it into evidence. ``lat``/``lon`` live here because ``organisations`` has no coordinates.
    """

    __tablename__ = "organisation_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    adapter_id: Mapped[str] = mapped_column(String(80))
    source_ref: Mapped[str] = mapped_column(String(300), default="", server_default=sa_text("''"))
    source_url: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(String(200))
    attribution: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    website: Mapped[str | None] = mapped_column(Text)
    email_published: Mapped[str | None] = mapped_column(String(320))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="SET NULL"))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("organisation_id", "adapter_id", "source_ref", name="uq_orgsource"),)


# --------------------------------------------------------------------------- discovery intelligence (addendum)
class BusinessRelationship(Base):
    """Edge in the business graph. Legal entity, trading brand, branch and domain are separate linked records."""

    __tablename__ = "business_relationships"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    # branch_of|brand_of|subsidiary_of|supplier_of|partner_of|franchise_of|member_of_group|exhibits_with|awarded_by|same_group
    relation: Mapped[str] = mapped_column(String(24))
    source_adapter: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=sa_text("0.5"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (
        UniqueConstraint("from_org_id", "to_org_id", "relation", "source_adapter", name="uq_bizrel"),
        CheckConstraint("from_org_id <> to_org_id", name="ck_bizrel_noself"),
    )


class OrganisationRecordLink(Base):
    """Cross-source resolution: every source record that was merged into / linked to an organisation (never silently lost)."""

    __tablename__ = "organisation_record_links"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    source_adapter: Mapped[str] = mapped_column(String(80))
    source_ref: Mapped[str] = mapped_column(String(300))
    identifier_type: Mapped[str | None] = mapped_column(String(30))  # company_number|lei|abn|charity_no|osm|wikidata|ted|...
    identifier_value: Mapped[str | None] = mapped_column(String(120))
    match_basis: Mapped[str] = mapped_column(String(40))  # registration_number|lei|domain|name_geo|manual
    match_confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=sa_text("0.5"))
    status: Mapped[str] = mapped_column(String(16), default="linked", server_default=sa_text("'linked'"))  # linked|review|rejected
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    # review/rejected links: the OTHER organisation record the source record was found to resemble (DISC-12)
    candidate_org_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    __table_args__ = (
        UniqueConstraint("organisation_id", "source_adapter", "source_ref", name="uq_orglink"),
        Index("ix_orglink_ident", "identifier_type", "identifier_value"),
        Index("ix_orglink_status", "status", "id"),
    )


class BusinessSignal(Base):
    __tablename__ = "business_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    # new_location|new_service|expansion|hiring|contract_award|supplier_partnership|event|accreditation|new_registration|exhibitor|sponsor
    signal_type: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(400))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    source_adapter: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)  # mandatory: no source URL, no signal
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default=sa_text("0.5"))
    dedupe_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("organisation_id", "dedupe_hash", name="uq_signal_dedupe"),)


class WebsiteSnapshot(Base):
    """Content fingerprints of monitored pages (DISC-10)."""

    __tablename__ = "website_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("organisation_domains.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    page_kind: Mapped[str] = mapped_column(String(16))  # contact|team|locations|services|home|other
    content_hash: Mapped[str] = mapped_column(String(64))
    emails_hash: Mapped[str | None] = mapped_column(String(64))
    emails: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    changed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    change_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    __table_args__ = (Index("ix_snapshot_url_time", "domain_id", "url", "observed_at"),)


class DocumentRecord(Base):
    """DISC-11: extracted record <-> exact source document location."""

    __tablename__ = "document_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_adapter: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64))
    doc_type: Mapped[str] = mapped_column(String(12))  # pdf|csv|xlsx|xml|json|vcard|html
    locator: Mapped[str] = mapped_column(String(200))  # page 3 / sheet1!B7 / row 42 / xpath
    organisation_id: Mapped[int | None] = mapped_column(ForeignKey("organisations.id", ondelete="SET NULL"), index=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("email_addresses.id", ondelete="SET NULL"))
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    parser_version: Mapped[str] = mapped_column(String(20), default="1", server_default=sa_text("'1'"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    __table_args__ = (UniqueConstraint("file_sha256", "locator", name="uq_docrec"),)


class OrganisationCompleteness(Base):
    """DISC-14: independent research-completeness flags; tri-state so 'not yet researched' != 'unavailable'."""

    __tablename__ = "organisation_completeness"
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True)
    # each: unknown (not researched) | yes | no (researched, genuinely not found / unavailable) | in_progress
    official_website: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    contact_page_inspected: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    published_email: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    contact_person_corroborated: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    email_infrastructure_checked: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    recipient_evidence: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    campaign_eligibility: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    recently_checked: Mapped[str] = mapped_column(String(12), default="unknown", server_default=sa_text("'unknown'"))
    gap_steps_tried: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))  # DISC-13 memory
    # DISC-13: per-attempt log [{step, at, outcome, yield, requests}] and ladder state (additive)
    gap_log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"))
    gap_exhausted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    score: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), onupdate=_now())


class SourceEffectiveness(Base):
    """Rolling per-source (and per source+run) counters used by the scheduler; computed from measured work only."""

    __tablename__ = "source_effectiveness"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(80))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="CASCADE"))
    records_seen: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    unique_businesses: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    duplicates: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    confirmed_domains: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    published_emails: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    eligible_contacts: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    http_requests: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    jobs: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    cpu_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text("0"))
    wall_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text("0"))
    peak_rss_mb: Mapped[float] = mapped_column(Float, default=0.0, server_default=sa_text("0"))
    priority_score: Mapped[float] = mapped_column(Float, default=1.0, server_default=sa_text("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now(), onupdate=_now())
    __table_args__ = (
        UniqueConstraint("adapter_id", "run_id", name="uq_srceff"),
        # run_id NULL = all-time row; NULLs are distinct in a plain unique constraint, so add a partial index
        Index("uq_srceff_global", "adapter_id", unique=True, postgresql_where=text("run_id is null")),
    )


class MonitorSchedule(Base):
    """DISC-08/10 recurring monitoring subscriptions (new registrations, website change checks)."""

    __tablename__ = "monitor_schedules"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))  # new_registrations|website_changes|signals
    name: Mapped[str] = mapped_column(String(200))
    frequency: Mapped[str] = mapped_column(String(10))  # daily|weekly|monthly
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
