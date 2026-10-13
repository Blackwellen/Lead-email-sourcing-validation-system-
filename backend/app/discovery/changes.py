"""DISC-10: website change detection.

Previously discovered contact / team / locations / services pages are re-fetched with conditional requests
(``If-None-Match`` / ``If-Modified-Since`` from the last snapshot) and compared by CONTENT FINGERPRINT: a hash of the
normalised visible text (scripts, styles, nav, comments, timestamps and copyright years ignored) plus a hash of the
extracted e-mail set. Every check writes a NEW ``website_snapshots`` row - prior snapshots are never overwritten.

* e-mails added   -> ``persist_observations`` (evidence on the page) and, for new/unvalidated addresses, a ``verify`` job
* e-mails removed -> the address and all its history are KEPT; a ``page_removed`` evidence row with context
  ``no_longer_published_on_page`` is added and ``last_seen_at`` is deliberately NOT refreshed (it goes stale)
* team members / locations are diffed from structured extraction (JSON-LD ``Person`` / team cards / ``address`` / location lists)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .. import jobqueue as jq
from ..crawler.extract import extract_from_html, looks_like_person_name
from ..crawler.ratelimit import RateLimiter
from ..crawler.robots import Decision, RobotsCache
from ..crawler.safe_http import SafeFetcher
from ..models import (
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    OrganisationDomain,
    WebsiteSnapshot,
)
from ..pipeline.ingest import normalise_email, persist_observations
from .htmlutil import clean, jsonld_nodes, node_types, parse, text_of

PARSER_VERSION = "changes-1"
ADAPTER = "website_changes"
PAGE_KIND_ORDER = ("contact", "team", "locations", "services", "home", "other")
_KIND_RX = [
    ("contact", re.compile(r"contact|get-in-touch|reach-us|enquir", re.I)),
    ("team", re.compile(r"team|people|staff|leadership|management|our-people|meet-|directors|partners|attorneys|solicitors", re.I)),
    ("locations", re.compile(r"location|branch|office|find-us|stores|depots|showrooms|clinics", re.I)),
    ("services", re.compile(r"service|what-we-do|solutions|products|treatments|specialis", re.I)),
]
_NOISE = [
    re.compile(r"(?:©|&copy;|\(c\))\s*(?:copyright\s*)?(?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?", re.I),
    re.compile(r"\bcopyright\s+(?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),
    re.compile(r"\b(?:last )?(?:updated|modified)[:\s]+[^.|]{0,30}", re.I),
]


def page_kind(url: str) -> str:
    path = urlsplit(url).path.lower()
    if path in ("", "/"):
        return "home"
    for kind, rx in _KIND_RX:
        if rx.search(path):
            return kind
    return "other"


def normalised_text(html: str) -> str:
    """Visible text with boilerplate / volatile noise removed - the basis of the content fingerprint."""
    tree = parse(html)
    if tree is None:
        return ""
    try:
        tree.strip_tags(["script", "style", "noscript", "svg", "iframe", "template", "nav", "form", "select", "option", "button"])
        body = tree.body or tree.root
        t = clean(body.text(separator=" ", strip=True)) if body is not None else ""
    except Exception:
        return ""
    for rx in _NOISE:
        t = rx.sub(" ", t)
    return clean(t).casefold()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def extract_members(html: str) -> list[str]:
    """Team members as ``"name|title"``: JSON-LD Person nodes and team cards whose heading looks like a person's name."""
    out: set[str] = set()
    tree = parse(html)
    if tree is None:
        return []
    for node in jsonld_nodes(html, tree):
        if "person" in node_types(node):
            n = clean(str(node.get("name") or ""))
            if n and looks_like_person_name(n):
                out.add(f"{n}|{clean(str(node.get('jobTitle') or ''))}".casefold())
    for card in tree.css("[class*=team], [class*=staff], [class*=member], [class*=person], [class*=people]"):
        h = card.css_first("h2, h3, h4, h5, strong")
        if h is None:
            continue
        n = text_of(h, 80)
        if n and looks_like_person_name(n):
            t = card.css_first("[class*=title], [class*=role], [class*=position], p, span")
            title = text_of(t, 80) if t is not None and t is not h else ""
            out.add(f"{n}|{title if title != n else ''}".casefold())
    return sorted(out)[:300]


def extract_locations(html: str) -> list[str]:
    out: set[str] = set()
    tree = parse(html)
    if tree is None:
        return []
    for node in jsonld_nodes(html, tree):
        addr = node.get("address")
        for a in addr if isinstance(addr, list) else [addr]:
            if isinstance(a, dict):
                s = " ".join(clean(str(a.get(k) or "")) for k in ("streetAddress", "addressLocality", "postalCode")).strip()
                if len(s) > 4:
                    out.add(re.sub(r"\s+", " ", s).casefold())
    for a in tree.css("address"):
        t = text_of(a, 200)
        if len(t) > 4:
            out.add(re.sub(r"\s+", " ", t).casefold())
    for ul in tree.css("[class*=location] li, [class*=branch] li, [class*=office] li"):
        t = text_of(ul, 120)
        if 2 < len(t) <= 100:
            out.add(re.sub(r"\s+", " ", t).casefold())
    return sorted(out)[:200]


@dataclass
class PageChange:
    url: str
    kind: str
    status: int | None = None
    not_modified: bool = False
    first_observation: bool = False
    content_changed: bool = False
    emails_added: list[str] = field(default_factory=list)
    emails_removed: list[str] = field(default_factory=list)
    members_added: list[str] = field(default_factory=list)
    members_removed: list[str] = field(default_factory=list)
    locations_added: list[str] = field(default_factory=list)
    locations_removed: list[str] = field(default_factory=list)
    snapshot_id: int | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.content_changed or self.emails_added or self.emails_removed or self.members_added or self.members_removed
                    or self.locations_added or self.locations_removed)


@dataclass
class ChangeReport:
    domain_id: int
    pages: list[PageChange] = field(default_factory=list)
    requests: int = 0
    verify_jobs: int = 0
    emails_added: list[str] = field(default_factory=list)
    emails_removed: list[str] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(p.changed for p in self.pages if not p.first_observation) or bool(self.emails_added or self.emails_removed)


async def _latest_snapshots(session: AsyncSession, domain_id: int) -> dict[str, WebsiteSnapshot]:
    """Newest snapshot per URL (ids are monotonic with observation time)."""
    latest = select(func.max(WebsiteSnapshot.id)).where(WebsiteSnapshot.domain_id == domain_id).group_by(WebsiteSnapshot.url)
    rows = (await session.execute(select(WebsiteSnapshot).where(WebsiteSnapshot.id.in_(latest)))).scalars().all()
    return {r.url: r for r in rows}


async def _known_pages(session: AsyncSession, dom: OrganisationDomain, prior: dict[str, WebsiteSnapshot], max_pages: int) -> list[str]:
    urls = list(prior)
    if not urls:  # never monitored: start from where e-mails were previously found on this site, else the home page
        host = re.escape(dom.domain.lower().removeprefix("www."))
        rows = (await session.execute(
            select(EvidenceSource.source_url).join(ContactEvidence, ContactEvidence.evidence_id == EvidenceSource.id)
            .where(ContactEvidence.organisation_id == dom.organisation_id, EvidenceSource.source_url.is_not(None),
                   EvidenceSource.evidence_type != "page_removed")
            .group_by(EvidenceSource.source_url).limit(50))).scalars().all()
        urls = [u for u in rows if re.search(rf"^https?://(?:www\.)?{host}(?:[:/]|$)", u or "", re.I)]
        if not urls:
            urls = [dom.website_url or f"https://{dom.domain}/"]
    urls = sorted(dict.fromkeys(urls), key=lambda u: (PAGE_KIND_ORDER.index(page_kind(u)), len(u), u))
    return urls[:max_pages]


async def check_site_for_changes(
    session: AsyncSession, fetcher: SafeFetcher, domain_row: OrganisationDomain, *, run_id: int | None,
    limiter: RateLimiter | None = None, robots: RobotsCache | None = None, max_pages: int = 6, now: datetime | None = None,
) -> ChangeReport:
    """Re-check the monitored pages of one domain and persist snapshots, diffs, new e-mails and removal evidence."""
    now = now or datetime.now(UTC)
    limiter = limiter or RateLimiter(min_interval=1.0)
    robots = robots or RobotsCache(fetcher)
    report = ChangeReport(domain_id=domain_row.id)
    prior = await _latest_snapshots(session, domain_row.id)
    urls = await _known_pages(session, domain_row, prior, max_pages)
    current_all: set[str] = set()
    observations_by_url: dict[str, list] = {}
    removed_by_url: dict[str, tuple[WebsiteSnapshot | None, list[str]]] = {}
    baseline_emails: set[str] = set()
    new_snaps: dict[str, WebsiteSnapshot] = {}
    for url in urls:
        chk = await robots.check(url)
        if chk.decision is not Decision.ALLOWED:
            report.skipped.append({"url": url, "reason": "robots_" + chk.decision.value})
            continue
        prev = prior.get(url)
        hdr: dict[str, str] = {}
        if prev is not None and prev.etag:
            hdr["If-None-Match"] = prev.etag
        if prev is not None and prev.last_modified:
            hdr["If-Modified-Since"] = prev.last_modified
        async with limiter.acquire(urlsplit(url).netloc) as slot:
            res = await fetcher.fetch(url, extra_headers=hdr or None, conditional=False)
            slot.report(res.status, res.retry_after() if res.status in (429, 503) else None)
        report.requests += 1
        pc = PageChange(url=url, kind=prev.page_kind if prev is not None else page_kind(url), status=res.status)
        report.pages.append(pc)
        if res.status == 304 and prev is not None:
            pc.not_modified = True
            snap = WebsiteSnapshot(
                domain_id=domain_row.id, url=url, page_kind=prev.page_kind, content_hash=prev.content_hash, emails_hash=prev.emails_hash,
                emails=list(prev.emails or []), etag=prev.etag, last_modified=prev.last_modified, observed_at=now, changed=False,
                change_summary={"diff": {"not_modified": True}, "state": (prev.change_summary or {}).get("state", {}),
                                "http": {"status": 304}, "parser_version": PARSER_VERSION})
            session.add(snap)
            new_snaps[url] = snap
            current_all |= set(prev.emails or [])
            continue
        if not res.usable_body or (res.content_type and "html" not in res.content_type):
            pc.error = res.error_code or f"status_{res.status}"
            continue
        html = res.text()
        final = res.final_url or url
        ex = extract_from_html(html, final)
        obs = [o for o in ex.emails if normalise_email(o.email)]
        emails = sorted({normalise_email(o.email) for o in obs})  # type: ignore[misc]
        members, locations = extract_members(html), extract_locations(html)
        chash = sha(normalised_text(html))
        pe = set(prev.emails or []) if prev is not None else set()
        pstate = (prev.change_summary or {}).get("state", {}) if prev is not None else {}
        pc.first_observation = prev is None
        pc.content_changed = prev is not None and prev.content_hash != chash
        pc.emails_added = sorted(set(emails) - pe) if prev is not None else []
        pc.emails_removed = sorted(pe - set(emails)) if prev is not None else []
        if prev is not None:
            pm, pl = set(pstate.get("members", [])), set(pstate.get("locations", []))
            pc.members_added, pc.members_removed = sorted(set(members) - pm), sorted(pm - set(members))
            pc.locations_added, pc.locations_removed = sorted(set(locations) - pl), sorted(pl - set(locations))
        diff = {k: getattr(pc, k) for k in ("emails_added", "emails_removed", "members_added", "members_removed", "locations_added",
                                            "locations_removed")}
        diff.update(content_changed=pc.content_changed, first_observation=pc.first_observation)
        snap = WebsiteSnapshot(
            domain_id=domain_row.id, url=url, page_kind=pc.kind, content_hash=chash, emails_hash=sha("\n".join(emails)), emails=emails,
            etag=(res.headers.get("etag") or None) and res.headers["etag"][:300],
            last_modified=(res.headers.get("last-modified") or None) and res.headers["last-modified"][:100], observed_at=now,
            changed=pc.changed and not pc.first_observation,
            change_summary={"diff": diff, "state": {"members": members, "locations": locations}, "http": {"status": res.status},
                            "parser_version": PARSER_VERSION})
        session.add(snap)
        new_snaps[url] = snap
        current_all |= set(emails)
        observations_by_url[url] = [o for o in obs if prev is None or normalise_email(o.email) in set(pc.emails_added)]
        if prev is None:
            baseline_emails |= set(emails)
        if pc.emails_removed:
            removed_by_url[url] = (prev, pc.emails_removed)
    await session.flush()
    for url, snap in new_snaps.items():
        for pc in report.pages:
            if pc.url == url:
                pc.snapshot_id = snap.id

    # ---- added e-mails: ingest + verify
    to_ingest = [o for obs in observations_by_url.values() for o in obs]
    if to_ingest:
        results = await persist_observations(session, domain_row.organisation_id, domain_row, to_ingest, run_id=run_id,
                                             source_adapter=ADAPTER, now=now)
        added_all = {e for pc in report.pages for e in pc.emails_added}
        ids = [i for i, _c in results]
        rows = {r.id: r for r in (await session.execute(select(EmailAddress).where(EmailAddress.id.in_(ids)))).scalars()} if ids else {}
        for email_id, created in results:
            e = rows.get(email_id)
            if e is None:
                continue
            is_added = e.email_normalised in added_all
            if is_added:
                report.emails_added.append(e.email_normalised)
            elif e.email_normalised not in baseline_emails:
                continue
            # added addresses are verified when new or never validated; baseline (first-look) addresses only when new to the system
            if (created or (is_added and e.last_validated_at is None)) and run_id is not None:
                if await jq.enqueue(session, run_id=run_id, stage="verify", dedupe_key=f"chg:{email_id}",
                                    payload={"email_id": email_id, "mode": "fast", "source": ADAPTER}, priority=55):
                    report.verify_jobs += 1

    # ---- removed e-mails: keep everything, add removal evidence
    for url, (prev, removed) in removed_by_url.items():
        snap = new_snaps[url]
        for em in removed:
            email_row = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == em))).scalar_one_or_none()
            if email_row is None:
                continue
            report.emails_removed.append(em)
            ev = pg_insert(EvidenceSource).values(
                source_adapter=ADAPTER, source_url=url, evidence_type="page_removed", observed_at=now, parser_version=PARSER_VERSION,
                content_hash=sha(f"{em}|{prev.id if prev else ''}"), source_reliability=0.5)
            ev_id = (await session.execute(ev.on_conflict_do_update(
                constraint="uq_evidence_identity", set_={"observed_at": now}).returning(EvidenceSource.id))).scalar_one()
            ctx = {"no_longer_published_on_page": True, "page_url": url, "previous_snapshot_id": prev.id if prev else None,
                   "snapshot_id": snap.id, "still_published_elsewhere_on_site": em in current_all,
                   "last_seen_at_preserved": email_row.last_seen_at.isoformat() if email_row.last_seen_at else None}
            cev = pg_insert(ContactEvidence).values(
                email_id=email_row.id, contact_id=email_row.contact_id, organisation_id=email_row.organisation_id, evidence_id=ev_id,
                run_id=run_id, extraction_method="removed", context=ctx, observed_at=now)
            await session.execute(cev.on_conflict_do_update(constraint="uq_contact_evidence", set_={"context": ctx, "observed_at": now}))
    domain_row.last_checked_at = now
    await session.flush()
    return report


# --------------------------------------------------------------------------- scheduling
async def schedule_change_checks(
    session: AsyncSession, *, older_than_days: int = 30, limit: int = 100, run_id: int | None = None, now: datetime | None = None,
) -> list[int]:
    """Domain ids due for a re-check: official (confirmed/high, not parked) domains whose last snapshot is older than
    ``older_than_days`` (or that were never snapshotted). Domains of organisations with published e-mails come first,
    then the stalest. With ``run_id`` a ``change_check`` job is queued per domain (idempotent per day)."""
    now = now or datetime.now(UTC)
    cut = now - timedelta(days=older_than_days)
    rows = (await session.execute(text(
        """SELECT d.id FROM organisation_domains d JOIN organisations o ON o.id = d.organisation_id
           LEFT JOIN LATERAL (SELECT max(observed_at) AS m FROM website_snapshots s WHERE s.domain_id = d.id) ls ON true
           LEFT JOIN LATERAL (SELECT count(*) AS c FROM email_addresses e WHERE e.organisation_id = o.id
                              AND e.origin IN ('published','corroborated')) ec ON true
           WHERE d.verified_match_status IN ('confirmed','high') AND NOT d.parked AND o.merged_into_id IS NULL
             AND (ls.m IS NULL OR ls.m < :cut)
           ORDER BY (ec.c > 0) DESC, ls.m ASC NULLS FIRST, d.id LIMIT :n"""), {"cut": cut, "n": max(1, limit)})).all()
    ids = [r[0] for r in rows]
    if run_id is not None:
        for i in ids:
            await jq.enqueue(session, run_id=run_id, stage="change_check", dedupe_key=f"chg:{i}:{now.date().isoformat()}",
                             payload={"domain_id": i}, priority=90)
    return ids


async def run_monitor(session: AsyncSession, fetcher: SafeFetcher, monitor: Any, *, limiter: RateLimiter | None = None,
                      robots: RobotsCache | None = None) -> dict[str, Any]:
    """MonitorSchedule kind='website_changes'. config: ``older_than_days`` (default from frequency), ``limit`` (25), ``max_pages`` (6)."""
    from ..system_runs import ensure_system_run

    cfg = dict(getattr(monitor, "config", None) or {})
    default_days = {"daily": 1, "weekly": 7, "monthly": 30}.get(getattr(monitor, "frequency", "weekly"), 7)
    ids = await schedule_change_checks(session, older_than_days=int(cfg.get("older_than_days", default_days)),
                                       limit=int(cfg.get("limit", 25)))
    run_id = await ensure_system_run(session, "website change monitoring") if ids else None
    limiter = limiter or RateLimiter(min_interval=1.0)
    robots = robots or RobotsCache(fetcher)
    stats = {"domains": 0, "pages": 0, "changed_pages": 0, "emails_added": 0, "emails_removed": 0, "verify_jobs": 0, "requests": 0}
    for i in ids:
        dom = await session.get(OrganisationDomain, i)
        if dom is None:
            continue
        rep = await check_site_for_changes(session, fetcher, dom, run_id=run_id, limiter=limiter, robots=robots,
                                           max_pages=int(cfg.get("max_pages", 6)))
        stats["domains"] += 1
        stats["pages"] += len(rep.pages)
        stats["changed_pages"] += sum(1 for p in rep.pages if p.changed and not p.first_observation)
        stats["emails_added"] += len(rep.emails_added)
        stats["emails_removed"] += len(rep.emails_removed)
        stats["verify_jobs"] += rep.verify_jobs
        stats["requests"] += rep.requests
    return stats

