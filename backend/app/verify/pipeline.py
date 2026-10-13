"""Verification orchestration: ``verify_email`` (FAST | DEEP | CUSTOM).

Each technical status is stored independently (syntax, domain mail, SMTP recipient evidence, catch-all,
overall reachability) next to a separate marketing status. Re-running is safe: network stages reuse
fresh, unexpired ``ValidationResult`` rows (and the shared ``DomainCache``), and identical results are
not inserted twice.

Nothing here claims inbox delivery. SMTP stages run only for explicitly authorised domains.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

import dns.asyncresolver
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..models import (
    Contact,
    ContactEvidence,
    DomainCache,
    EmailAddress,
    EvidenceSource,
    Organisation,
    OrganisationDomain,
    SuppressionEntry,
    ValidationResult,
)
from . import lists
from .catchall import CatchAllResult, assess_catch_all
from .delivery import delivery_history, domain_accept_then_bounce
from .dns_health import check_resolver
from .dnsmx import DomainMailInfo, configure_concurrency, get_domain_info
from .marketing import marketing_decision
from .scoring import SOURCE_POINTS, ScoreInputs, ScoreResult, score_email
from .smtp import ProbeResult, SmtpProbe, is_probe_authorised
from .syntax import SyntaxResult, check_syntax

VERIFIER_VERSION = "2.0"
Mode = Literal["fast", "deep", "custom"]


@dataclass(slots=True)
class VerifyOptions:
    """Per-stage switches and limits. ``VerifyOptions.for_mode`` gives the FAST / DEEP presets."""

    syntax: bool = True
    dns: bool = True
    flags: bool = True  # V05 disposable / free / role
    provider: bool = False  # record MX provider classification on V03
    catch_all: bool = False  # V06 (authorised domains only)
    smtp: bool = False  # V07-V09 (authorised domains only)
    delivery: bool = False  # V10 history
    dns_diagnostics: bool = False  # SPF / DMARC presence (never affects existence)
    dns_concurrency: int = 20
    smtp_concurrency: int = 2
    domain_cache_ttl_floor: int = 300
    domain_cache_ttl_ceil: int = 86400
    revalidation_interval_hours: float = 24 * 7
    confidence_threshold: int = 70
    dns_timeout_s: float = 5.0
    smtp_connect_timeout_s: float = 10.0
    smtp_command_timeout_s: float = 15.0
    smtp_max_retries: int = 2
    smtp_backoff_base_s: float = 2.0
    smtp_backoff_max_s: float = 10.0
    disposable_reachability: Literal["risky", "invalid"] = "risky"
    force: bool = False  # ignore fresh stored results

    @classmethod
    def for_mode(cls, mode: Mode) -> VerifyOptions:
        if mode == "fast":
            return cls()
        if mode == "deep":
            return cls(provider=True, catch_all=True, smtp=True, delivery=True, dns_diagnostics=True)
        return cls()


@dataclass(slots=True)
class VerifyOutcome:
    email_id: int
    mode: str
    syntax_status: str
    domain_mail_status: str
    recipient_evidence_status: str
    catch_all_status: str
    overall_reachability: str
    confidence: int
    marketing_status: str
    marketing_reason: str = ""
    breakdown: dict[str, Any] = field(default_factory=dict)
    caps_applied: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    checks_reused: list[str] = field(default_factory=list)
    network_skipped: bool = False
    notes: list[str] = field(default_factory=list)


SmtpFactory = Callable[[VerifyOptions], Any]
_smtp_sem: dict[str, Any] = {"loop": None, "sem": None, "limit": 0}
_domain_locks: dict[str, asyncio.Lock] = {}


def _smtp_semaphore(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if _smtp_sem["sem"] is None or _smtp_sem["loop"] is not loop or _smtp_sem["limit"] != limit:
        _smtp_sem.update(loop=loop, sem=asyncio.Semaphore(max(1, limit)), limit=limit)
    return _smtp_sem["sem"]


def default_smtp_factory(settings: Settings | None = None) -> SmtpFactory:
    def make(o: VerifyOptions) -> SmtpProbe:
        return SmtpProbe(
            settings=settings, connect_timeout=o.smtp_connect_timeout_s, command_timeout=o.smtp_command_timeout_s,
            max_deferral_retries=o.smtp_max_retries, backoff_base=o.smtp_backoff_base_s, backoff_max=o.smtp_backoff_max_s,
        )
    return make


# --------------------------------------------------------------------------- cost estimate
def estimate_cost(options: VerifyOptions | None = None, *, mode: Mode | None = None, count: int = 1) -> dict[str, Any]:
    """Human-readable resource cost and limitations for the UI (per ``count`` addresses)."""
    o = options or VerifyOptions.for_mode(mode or "fast")
    res: list[str] = []
    per_email: dict[str, Any] = {"dns_queries": 0, "smtp_connections": 0}
    if o.syntax:
        res.append("Syntax and normalisation: instant, local CPU only.")
    if o.flags:
        res.append("Disposable / free-provider / role checks: instant, local lists.")
    if o.dns:
        per_email["dns_queries"] = 3 + (2 if o.dns_diagnostics else 0)
        res.append(f"DNS: roughly 3 queries per new domain (MX plus A/AAAA of MX hosts), cached for {o.domain_cache_ttl_floor}-{o.domain_cache_ttl_ceil}s "
                   f"and shared by every address on the domain; up to {o.dns_concurrency} lookups in parallel.")
    if o.catch_all or o.smtp:
        per_email["smtp_connections"] = int(o.smtp) + int(o.catch_all)
        res.append(f"SMTP diagnostics: up to {per_email['smtp_connections']} short connection(s) per address, only to explicitly authorised domains, "
                   f"{o.smtp_concurrency} at a time, {o.smtp_connect_timeout_s:g}s connect / {o.smtp_command_timeout_s:g}s command timeouts, "
                   f"up to {o.smtp_max_retries} backed-off retries on temporary replies. No message is ever sent (no DATA).")
    if o.delivery:
        res.append("Delivery history: one database query over imported feedback.")
    limits = [
        "Results are evidence, not a promise: no check can guarantee a message reaches an inbox.",
        "A server accepting a recipient at SMTP time (250) is recorded as 'smtp_recipient_accepted', never as 'deliverable'.",
        "Catch-all domains accept any address, so SMTP acceptance there proves nothing about the mailbox.",
        "Unauthorised third-party domains are never probed over SMTP; they get DNS/MX evidence only.",
        "Many networks block outbound port 25 and many providers greylist or hide mailbox existence, so SMTP results are often inconclusive.",
        "DNS timeouts are reported as temporary failures, never as invalid.",
        "Marketing status is a policy triage independent of these checks and is not legal advice.",
    ]
    total_note = f"For {count} address(es): about {count * per_email['dns_queries']} DNS queries worst case (fewer with domain caching) and {count * per_email['smtp_connections']} SMTP connections worst case."
    return {"summary": " ".join(res), "resources": res, "per_email": per_email, "total_note": total_note, "limitations": limits,
            "options": asdict(o)}


# --------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(UTC)


# Per-call cache of the latest ValidationResult per check type (one query instead of one per stage).
_LATEST: ContextVar[tuple[int, dict[str, ValidationResult]] | None] = ContextVar("_verify_latest_cache", default=None)


async def _preload_latest(session: AsyncSession, email_id: int) -> dict[str, ValidationResult]:
    rows = (await session.execute(
        select(ValidationResult).where(ValidationResult.email_id == email_id)
        .order_by(ValidationResult.check_type, ValidationResult.observed_at.desc(), ValidationResult.id.desc())
        .ext(distinct_on(ValidationResult.check_type))
    )).scalars().all()
    return {r.check_type: r for r in rows}


async def _latest(session: AsyncSession, email_id: int, check_type: str) -> ValidationResult | None:
    cached = _LATEST.get()
    if cached is not None and cached[0] == email_id:
        return cached[1].get(check_type)
    return (await session.execute(
        select(ValidationResult).where(ValidationResult.email_id == email_id, ValidationResult.check_type == check_type)
        .order_by(ValidationResult.observed_at.desc(), ValidationResult.id.desc()).limit(1)
    )).scalar_one_or_none()


def _fresh(row: ValidationResult | None, now: datetime) -> bool:
    return row is not None and row.verifier_version == VERIFIER_VERSION and row.expires_at is not None and row.expires_at > now


async def _record(
    session: AsyncSession, email_id: int, check_type: str, result: str, reason: str | None, *, detail: dict[str, Any] | None = None,
    response_code: int | None = None, enhanced: str | None = None, provider: str | None = None, ttl: timedelta | None = None,
    permission_context: str | None = None,
) -> ValidationResult:
    """Insert a ValidationResult unless an identical, unexpired one is already the latest."""
    now = _now()
    prev = await _latest(session, email_id, check_type)
    if (prev is not None and prev.result == result and prev.reason_code == reason and prev.response_code == response_code
            and prev.permission_context == permission_context and prev.verifier_version == VERIFIER_VERSION
            and prev.expires_at is not None and prev.expires_at > now and (prev.detail or {}) == (detail or {})):
        return prev
    row = ValidationResult(
        email_id=email_id, check_type=check_type, result=result, reason_code=reason, detail=detail or {}, response_code=response_code,
        enhanced_status_code=enhanced, provider=provider, observed_at=now, expires_at=now + (ttl or timedelta(days=30)),
        verifier_version=VERIFIER_VERSION, permission_context=permission_context,
    )
    session.add(row)  # flushed with the caller's transaction (no per-row round trip)
    cached = _LATEST.get()
    if cached is not None and cached[0] == email_id:
        cached[1][check_type] = row
    return row


def _source_kind(adapter: str | None, etype: str | None, url: str | None, reg_domain: str) -> str:
    a, t = (adapter or "").lower(), (etype or "").lower()
    if t == "permutation":
        return "inferred"
    if t == "manual":
        return "manual"
    if t == "dataset":
        return "dataset"
    if any(k in a for k in ("directory", "osm", "yell", "companies", "registry")):
        return "directory"
    host = (urlparse(url).hostname or "").lower() if url else ""
    if host and reg_domain and (host == reg_domain or host.endswith("." + reg_domain)):
        return "own_domain_page"
    return "third_party_page" if host else "directory"


async def _source_profile(session: AsyncSession, email: EmailAddress) -> dict[str, Any]:
    rows = (await session.execute(
        select(EvidenceSource, ContactEvidence.observed_at)
        .join(ContactEvidence, ContactEvidence.evidence_id == EvidenceSource.id)
        .where(ContactEvidence.email_id == email.id)
    )).all()
    if not rows:
        return {"kind": "directory" if email.origin in ("published", "corroborated") else "inferred", "reliability": 0.5, "count": 1,
                "last": email.last_seen_at}
    kinds = [(_source_kind(r.source_adapter, r.evidence_type, r.source_url, email.registrable_domain), r) for r, _ in rows]
    best = max(kinds, key=lambda kr: SOURCE_POINTS.get(kr[0], 0))
    distinct = {(r.source_adapter, r.source_url or r.source_document or r.id) for _, r in kinds}
    last = max([o for _, o in rows if o] + [r.observed_at for _, r in kinds if r.observed_at] or [email.last_seen_at])
    return {"kind": best[0], "reliability": max(r.source_reliability for _, r in kinds), "count": len(distinct), "last": last}


async def _identity(session: AsyncSession, email: EmailAddress) -> tuple[float, int]:
    rows = (await session.execute(
        select(OrganisationDomain).where(OrganisationDomain.organisation_id == email.organisation_id,
                                         OrganisationDomain.registrable_domain == email.registrable_domain)
    )).scalars().all()
    if not rows:
        return 0.0, 0
    best = 0.0
    conflicts = 0
    for r in rows:
        c = r.confidence or 0.0
        c = c / 100 if c > 1 else c
        if r.verified_match_status == "confirmed":
            c = max(c, 1.0)
        elif r.verified_match_status == "high":
            c = max(c, 0.85)
        elif r.verified_match_status == "mismatch":
            conflicts += 1
            c = 0.0
        best = max(best, c)
    return best, conflicts


async def _suppressions(session: AsyncSession, email: EmailAddress, org: Organisation | None) -> list[SuppressionEntry]:
    parts = email.domain.split(".")
    doms = {".".join(parts[i:]) for i in range(len(parts) - 1)} | {email.registrable_domain}
    company_vals = {str(org.id)} if org else set()
    if org:
        company_vals |= {v.lower() for v in (org.company_number, org.canonical_identity, org.legal_name, org.trading_name) if v}
    conds = [(SuppressionEntry.kind == "email") & (SuppressionEntry.value == email.email_normalised),
             (SuppressionEntry.kind == "domain") & (SuppressionEntry.value.in_(doms))]
    if company_vals:
        conds.append((SuppressionEntry.kind == "company") & (SuppressionEntry.value.in_(company_vals)))
    return list((await session.execute(select(SuppressionEntry).where(or_(*conds)))).scalars().all())


# --------------------------------------------------------------------------- main entry
async def verify_email(
    session: AsyncSession,
    email_id: int,
    *,
    mode: Mode = "fast",
    options: VerifyOptions | None = None,
    resolver: Any = None,
    smtp_factory: SmtpFactory | None = None,
    settings: Settings | None = None,
) -> VerifyOutcome:
    """Verify one stored address and persist results. Idempotent; never claims inbox delivery.

    ``mode`` picks the FAST / DEEP preset; explicit ``options`` (the CUSTOM case) always win over the preset.
    """
    token = _LATEST.set((email_id, await _preload_latest(session, email_id)))
    try:
        return await _verify_email(session, email_id, mode=mode, options=options, resolver=resolver, smtp_factory=smtp_factory,
                                   settings=settings)
    finally:
        _LATEST.reset(token)


async def _verify_email(
    session: AsyncSession,
    email_id: int,
    *,
    mode: Mode,
    options: VerifyOptions | None,
    resolver: Any,
    smtp_factory: SmtpFactory | None,
    settings: Settings | None,
) -> VerifyOutcome:
    st = settings or get_settings()
    o = replace(options) if options is not None else VerifyOptions.for_mode(mode)
    email = await session.get(EmailAddress, email_id)
    if email is None:
        raise LookupError(f"email {email_id} not found")
    org = await session.get(Organisation, email.organisation_id)
    now = _now()
    ran: list[str] = []
    reused: list[str] = []
    notes: list[str] = []
    network_skipped = True

    # ---- V01 syntax
    syn: SyntaxResult
    if o.syntax:
        syn = check_syntax(email.email_original or email.email_normalised, strip_display=True)
        await _record(session, email.id, "V01_syntax", syn.status, syn.reason_code,
                      detail={"normalised": syn.normalised, "ascii_domain": syn.ascii_domain, "error": syn.error, "smtputf8": syn.smtputf8},
                      ttl=timedelta(days=365))
        ran.append("V01_syntax")
    else:
        prev = await _latest(session, email.id, "V01_syntax")
        syn = SyntaxResult(prev.result if prev else "unknown", email.email_normalised, email.local_part, email.domain,
                           (prev.detail or {}).get("ascii_domain") if prev else email.domain)
        reused.append("V01_syntax")
    syntax_status = syn.status
    domain = (syn.ascii_domain or email.domain or "").lower()

    # ---- V05 flags
    is_disp, is_free, role = email.is_disposable, email.is_free_provider, email.role_type
    if o.flags and domain:
        is_disp, is_free = lists.is_disposable(domain), lists.is_free_provider(domain)
        role = lists.classify_role(syn.local_part or email.local_part or "")
        await _record(session, email.id, "V05_flags", "flagged" if (is_disp or is_free or role) else "clean", None,
                      detail={"disposable": is_disp, "free_provider": is_free, "role_type": role}, ttl=timedelta(days=30))
        ran.append("V05_flags")
        email.is_disposable, email.is_free_provider, email.role_type = is_disp, is_free, role

    # ---- V02/V03 domain
    mail_info: DomainMailInfo | None = None
    mail_status = "unknown"
    if o.dns and syntax_status == "valid" and domain:
        configure_concurrency(o.dns_concurrency)
        res = resolver
        if res is None:
            res = dns.asyncresolver.Resolver()
            res.timeout, res.lifetime = o.dns_timeout_s, o.dns_timeout_s * 2
        mail_info = await get_domain_info(session, domain, resolver=res, ttl_floor=o.domain_cache_ttl_floor,
                                          ttl_ceil=o.domain_cache_ttl_ceil, force_refresh=o.force, include_diagnostics=o.dns_diagnostics)
        if not mail_info.from_cache:
            network_skipped = False
        mail_status = mail_info.mail_status
        if resolver is None and mail_info.implicit_mx:
            # implicit MX is only meaningful if the resolver can answer MX queries at all
            health = await check_resolver(res)
            if not health.ok:
                mail_status = "unknown"
                notes.append(f"resolver_unreliable:{health.status}")
        ttl = timedelta(seconds=mail_info.ttl)
        v2 = ("nxdomain" if mail_info.reason_code == "domain_nxdomain" else "temporary_failure" if mail_status in ("temporary_dns_failure", "unknown")
              else "no_records" if mail_info.reason_code == "no_mx_no_address" else "resolves")
        await _record(session, email.id, "V02_domain", v2, mail_info.reason_code, ttl=ttl,
                      detail={"domain": domain, "error": mail_info.error, "dnssec": mail_info.dnssec})
        await _record(session, email.id, "V03_mx", mail_status, mail_info.reason_code, ttl=ttl,
                      provider=mail_info.provider if o.provider else None,
                      detail={"mx": mail_info.mx_dicts(), "implicit_mx": mail_info.implicit_mx, "diagnostics": mail_info.diagnostics})
        ran += ["V02_domain", "V03_mx"]
    elif syntax_status == "valid" and domain:
        v3 = await _latest(session, email.id, "V03_mx")
        if _fresh(v3, now) and v3 is not None:
            mail_status = v3.result
            reused.append("V03_mx")
        cached = await session.get(DomainCache, domain)
        if mail_status == "unknown" and cached is not None and cached.expires_at > now:
            mail_status = cached.mail_status
            reused.append("domain_cache")

    # ---- V07 / V06 (authorised only)
    recipient = "untested"
    catch_all = "untested"
    probe_res: ProbeResult | None = None
    ca_res: CatchAllResult | None = None
    wants_net = (o.smtp or o.catch_all) and syntax_status == "valid" and mail_status == "accepts_mail" and not is_disp
    probe: Any = None
    if wants_net:
        probe = (smtp_factory or default_smtp_factory(st))(o)
        pset = getattr(probe, "settings", st)
        auth = await is_probe_authorised(session, domain, settings=pset)
        if not auth:
            if o.smtp:
                await _record(session, email.id, "V07_smtp", "untested", auth.reason, detail={"note": "no SMTP connection was made"},
                              ttl=timedelta(hours=o.revalidation_interval_hours))
                ran.append("V07_smtp")
            if o.catch_all:
                ca_res = await assess_catch_all(session, domain, mail_info, probe=None,
                                                delivery_feedback_accept_then_bounce=await domain_accept_then_bounce(session, domain))
                catch_all = ca_res.status
                await _record(session, email.id, "V06_catchall", ca_res.status, ca_res.reason_code, detail=ca_res.detail(),
                              ttl=timedelta(days=7))
                ran.append("V06_catchall")
        else:
            async with _smtp_semaphore(o.smtp_concurrency):
                lock = _domain_locks.setdefault(domain, asyncio.Lock())
                async with lock:  # one probe at a time per domain: be polite
                    hosts = [h.host for h in (mail_info.mx_hosts_by_priority() if mail_info else [])]
                    interval = timedelta(hours=o.revalidation_interval_hours)
                    if o.smtp:
                        prev = await _latest(session, email.id, "V07_smtp")
                        if not o.force and _fresh(prev, now) and prev is not None and prev.result != "untested":
                            recipient = prev.result
                            reused.append("V07_smtp")
                        else:
                            probe_res = await probe.probe(session, email.email_normalised, hosts, domain=domain)
                            network_skipped = False
                            recipient = probe_res.recipient_evidence_status
                            conclusive = recipient in ("smtp_recipient_accepted", "smtp_recipient_rejected")
                            await _record(session, email.id, "V07_smtp", recipient, probe_res.reason_code,
                                          detail=probe_res.detail(), response_code=probe_res.response_code,
                                          enhanced=probe_res.enhanced_status_code, provider=mail_info.provider if mail_info else None,
                                          ttl=interval if conclusive else timedelta(hours=1), permission_context=probe_res.permission_context)
                            ran.append("V07_smtp")
                    if o.catch_all:
                        prev6 = await _domain_catchall(session, domain, now, include_passive=False)
                        if not o.force and prev6 is not None:
                            catch_all = prev6.result
                            reused.append("V06_catchall")
                        else:
                            ca_res = await assess_catch_all(
                                session, domain, mail_info, probe=probe,
                                delivery_feedback_accept_then_bounce=await domain_accept_then_bounce(session, domain))
                            network_skipped = network_skipped and ca_res.probe is None
                            catch_all = ca_res.status
                            await _record(session, email.id, "V06_catchall", ca_res.status, ca_res.reason_code, detail=ca_res.detail(),
                                          permission_context=ca_res.permission_context,
                                          ttl=timedelta(days=7) if ca_res.status != "unknown" else timedelta(hours=1))
                            ran.append("V06_catchall")
    # fall back to fresh stored rows for stages not run now
    if recipient == "untested":
        prev = await _latest(session, email.id, "V07_smtp")
        if _fresh(prev, now) and prev is not None and prev.permission_context:
            recipient = prev.result
            reused.append("V07_smtp")
    if catch_all == "untested":
        prev6 = await _domain_catchall(session, domain, now) if domain else None
        if prev6 is not None:
            catch_all = prev6.result
            reused.append("V06_catchall")
        else:
            own = await _latest(session, email.id, "V06_catchall")
            if _fresh(own, now) and own is not None:
                catch_all = own.result

    # ---- V10 delivery history (always applies stored bounce flag)
    hist: dict[str, int] = {}
    if o.delivery or email.previously_bounced:
        hist = await delivery_history(session, email.id)
    if o.delivery:
        await _record(session, email.id, "V10_delivery", "history" if hist else "none", None,
                      detail={"events": hist, "note": "'accepted' events never imply inbox placement"}, ttl=timedelta(days=1))
        ran.append("V10_delivery")

    # ---- score
    prof = await _source_profile(session, email)
    ident, ident_conflicts = await _identity(session, email)
    contact_conf = 0.0
    if email.contact_id:
        c = await session.get(Contact, email.contact_id)
        contact_conf = (c.identity_confidence if c else 0.0) or 0.0
        contact_conf = contact_conf / 100 if contact_conf > 1 else contact_conf
    last = prof["last"]
    age_days = max(0.0, (now - last).total_seconds() / 86400) if last else None
    inputs = ScoreInputs(
        origin=email.origin, source_kind=prof["kind"], source_reliability=prof["reliability"], domain_identity_confidence=ident,
        contact_confidence=contact_conf, corroborating_sources=prof["count"], syntax_status=syntax_status, domain_mail_status=mail_status,
        implicit_mx=bool(mail_info and mail_info.implicit_mx), recipient_evidence_status=recipient, catch_all_status=catch_all,
        age_days=age_days, previously_bounced=email.previously_bounced, hard_bounce=hist.get("hard_bounce", 0) > 0,
        soft_bounces=hist.get("soft_bounce", 0), complaint=hist.get("complaint", 0) > 0, conflicts=ident_conflicts,
        is_free_provider=is_free, is_disposable=is_disp, role_type=role, safe_threshold=o.confidence_threshold,
        disposable_reachability=o.disposable_reachability,
    )
    sr: ScoreResult = score_email(inputs)

    # ---- persist independent statuses
    email.syntax_status = syntax_status
    email.domain_mail_status = mail_status
    email.recipient_evidence_status = recipient
    email.catch_all_status = catch_all
    email.overall_reachability = sr.reachability
    email.confidence = float(sr.score)

    # ---- marketing (operator policy from the admin 'validation' settings; eligibility gated on origin / live evidence / reachability)
    sups = await _suppressions(session, email, org)
    md = marketing_decision(email, org, sups, await _marketing_settings(session, settings),
                            live_evidence=await _has_live_evidence(session, email))
    email.confidence_breakdown = {**sr.as_dict(), "verifier_version": VERIFIER_VERSION, "mode": mode, "checks_run": ran, "checks_reused": reused,
                                  "marketing_reason": md.reason}
    email.marketing_status = md.status
    email.needs_review = bool(ident_conflicts) or sr.reachability == "unknown"
    email.last_validated_at = now
    await session.commit()
    return VerifyOutcome(
        email.id, mode, syntax_status, mail_status, recipient, catch_all, sr.reachability, sr.score, md.status, md.reason,
        email.confidence_breakdown, sr.caps_applied, ran, reused, network_skipped, notes,
    )


async def _domain_catchall(session: AsyncSession, domain: str, now: datetime, *, include_passive: bool = True) -> ValidationResult | None:
    """Fresh domain-level catch-all result of any address on this domain.

    Conclusive results of an *active* (authorised) test win; otherwise (``include_passive``) a fresh passive 'suspected' verdict
    (e.g. accept-then-bounce delivery feedback) is shared by every address of the domain too, so a sibling address checked
    without the catch-all stage is not reported 'safe'. The authorised path passes ``include_passive=False`` so a passive
    suspicion never replaces a real test."""
    active = and_(ValidationResult.permission_context.is_not(None), ValidationResult.result.in_(("confirmed", "rejected_unknown_recipient")))
    cond = or_(active, ValidationResult.result == "suspected") if include_passive else active
    row = (await session.execute(
        select(ValidationResult).join(EmailAddress, EmailAddress.id == ValidationResult.email_id)
        .where(EmailAddress.domain == domain, ValidationResult.check_type == "V06_catchall", ValidationResult.expires_at > now,
               ValidationResult.verifier_version == VERIFIER_VERSION, cond)
        .order_by(ValidationResult.permission_context.is_not(None).desc(), ValidationResult.observed_at.desc()).limit(1)
    )).scalar_one_or_none()
    return row


# --------------------------------------------------------------------------- marketing policy / live evidence
_POLICY_CACHE: dict[str, Any] = {"at": -1e9, "value": None}
POLICY_CACHE_S = 10.0


def invalidate_marketing_policy_cache() -> None:
    _POLICY_CACHE.update(at=-1e9, value=None)


async def _marketing_settings(session: AsyncSession, settings: Any) -> Any:
    """Settings object for :func:`marketing_decision`: an explicit ``marketing_policy`` on ``settings`` wins; otherwise the
    operator's policy from the admin 'validation' settings section (cached for :data:`POLICY_CACHE_S` seconds)."""
    if getattr(settings, "marketing_policy", None):
        return settings
    import time

    now = time.monotonic()
    if _POLICY_CACHE["value"] is None or now - _POLICY_CACHE["at"] > POLICY_CACHE_S:
        from ..settings_schema import load_section

        sec = await load_section(session, "validation")
        _POLICY_CACHE.update(at=now, value={k: v.model_dump() for k, v in (sec.marketing_policy or {}).items()})
    return {"marketing_policy": _POLICY_CACHE["value"] or {}}


async def _has_live_evidence(session: AsyncSession, email: EmailAddress) -> bool | None:
    """True / False when the address has evidence rows (live = not archived and not a permutation guess); None when it has none."""
    from ..pipeline.ingest import live_evidence_exists

    any_ev = (await session.execute(select(ContactEvidence.id).where(ContactEvidence.email_id == email.id).limit(1))).first()
    if any_ev is None:
        return None
    return bool((await session.execute(select(live_evidence_exists(email.id)))).scalar())
