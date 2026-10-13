"""Data-quality workbench: issue counts, keyset-paginated issue lists and audited batch review actions.

Roles: viewer = read, operator = review actions, admin = ``merge`` (irreversible company merge; ``split`` can undo it from the audit
trail). Every action on every id writes an audit event holding ``before`` / ``after`` / ``reason``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, and_, case, exists, func, literal, not_, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..db import get_session
from ..discovery import resolution
from ..models import (
    Contact,
    EmailAddress,
    Organisation,
    OrganisationCompleteness,
    OrganisationDomain,
    OrganisationRecordLink,
    User,
)
from ..security import ROLE_RANK, ApiError, audit, require_role
from ..suppression import suppress
from .businesses import enqueue_refresh
from .common import Page, SortSpec, apply_keyset, capped_count, finish_page
from .contacts import enqueue_verify

log = logging.getLogger("api.data_quality")
router = APIRouter(prefix="/api/data-quality", tags=["data-quality"], dependencies=[Depends(require_role("viewer"))])

ISSUE_TYPES = ("duplicate_companies", "unconfirmed_domains", "missing_websites", "stale_emails", "conflicting_evidence", "incomplete_contacts",
               "catchall_or_unknown", "unresolved_identities")
IssueType = Literal["duplicate_companies", "unconfirmed_domains", "missing_websites", "stale_emails", "conflicting_evidence", "incomplete_contacts",
                    "catchall_or_unknown", "unresolved_identities"]
ACTIONS = ("confirm_duplicate", "merge", "reject_duplicate", "confirm_domain", "reject_domain", "queue_recrawl", "queue_revalidate", "mark_reviewed", "suppress")
ActionName = Literal["confirm_duplicate", "merge", "reject_duplicate", "confirm_domain", "reject_domain", "queue_recrawl", "queue_revalidate", "mark_reviewed", "suppress"]
SUBJECT_OF_ACTION = {
    "confirm_duplicate": "review_link", "merge": "review_link", "reject_duplicate": "review_link", "confirm_domain": "domain", "reject_domain": "domain",
    "queue_recrawl": "organisation", "queue_revalidate": "email", "mark_reviewed": "organisation", "suppress": "email",
}
CAP = 100_000
A, B = aliased(Organisation), aliased(Organisation)
L, D, E, O = OrganisationRecordLink, OrganisationDomain, EmailAddress, Organisation
SORT = {"id": SortSpec(literal(0), "int")}  # replaced per query (the sort column is the subject id)


# --------------------------------------------------------------------------- models
class IssueRow(BaseModel):
    id: int = Field(description="row key (stable, unique within the issue type); the id to pass to POST /actions for this issue type")
    issue_type: str
    subject_type: Literal["review_link", "organisation", "domain", "email", "contact"]
    subject_id: int
    organisation_id: int | None
    organisation_name: str | None
    summary: str
    classification: str | None = None
    detected_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class IssueCount(BaseModel):
    issue_type: str
    count: int | None
    capped: bool = False
    action_hint: str


class SummaryOut(BaseModel):
    generated_at: datetime
    issues: list[IssueCount]
    total: int


class ActionIn(BaseModel):
    action: ActionName
    ids: list[int] = Field(min_length=1, max_length=500, description="review-link ids | domain ids | organisation ids | email ids, depending on the action")
    reason: str = Field(min_length=3, max_length=500, description="required; stored in the audit trail")


class ActionResult(BaseModel):
    id: int
    status: Literal["ok", "skipped", "error"]
    message: str = ""
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class ActionOut(BaseModel):
    action: str
    subject_type: str
    requested: int
    ok: int
    skipped: int
    errors: int
    results: list[ActionResult]


HINTS = {
    "duplicate_companies": "confirm_duplicate / merge / reject_duplicate (ids = row ids)", "unconfirmed_domains": "confirm_domain / reject_domain / queue_recrawl",
    "missing_websites": "queue_recrawl / mark_reviewed", "stale_emails": "queue_revalidate / queue_recrawl / suppress", "conflicting_evidence": "mark_reviewed / queue_recrawl",
    "incomplete_contacts": "queue_recrawl / mark_reviewed", "catchall_or_unknown": "queue_revalidate / suppress", "unresolved_identities": "reject_duplicate / merge",
}


def _nm(o):
    return func.coalesce(o.legal_name, o.trading_name)


# --------------------------------------------------------------------------- issue queries
# Each builder returns (select with columns [key, ...payload], key column, mapper(row) -> IssueRow, detected-at column optional).
def _q_links(with_candidate: bool):
    cond = L.candidate_org_id.is_not(None) if with_candidate else L.candidate_org_id.is_(None)
    st = (select(L.id.label("k"), L.organisation_id, L.candidate_org_id, L.match_basis, L.match_confidence, L.source_adapter, L.source_ref, L.observed_at, L.detail,
                 _nm(A).label("a_name"), A.company_number.label("a_cn"), _nm(B).label("b_name"), B.company_number.label("b_cn"))
         .select_from(L).join(A, A.id == L.organisation_id).outerjoin(B, B.id == L.candidate_org_id)
         .where(L.status == "review", cond, A.merged_into_id.is_(None), or_(B.id.is_(None), B.merged_into_id.is_(None))))
    itype = "duplicate_companies" if with_candidate else "unresolved_identities"

    def mp(r) -> IssueRow:
        who = f"'{r.a_name}' resembles '{r.b_name}'" if r.candidate_org_id else f"record {r.source_adapter}:{r.source_ref} matches '{r.a_name}' ambiguously"
        return IssueRow(id=r.k, issue_type=itype, subject_type="review_link", subject_id=r.k, organisation_id=r.organisation_id, organisation_name=r.a_name,
                        summary=f"{who} ({r.match_basis}, confidence {r.match_confidence:.2f}); never merged automatically", detected_at=r.observed_at,
                        details={"basis": r.match_basis, "confidence": r.match_confidence, "adapter": r.source_adapter, "source_ref": r.source_ref,
                                 "candidate_org_id": r.candidate_org_id, "candidate_name": r.b_name, "organisation_company_number": r.a_cn,
                                 "candidate_company_number": r.b_cn, "confirmed_duplicate": bool((r.detail or {}).get("confirmed_duplicate"))})
    return st, L.id, mp


def _q_unconfirmed_domains():
    st = (select(D.id.label("k"), D.organisation_id, D.domain, D.verified_match_status, D.confidence, D.website_url, D.last_checked_at, D.updated_at,
                 func.jsonb_array_length(D.evidence).label("n_ev"), _nm(O).label("o_name"))
          .select_from(D).join(O, O.id == D.organisation_id)
          .where(O.merged_into_id.is_(None), D.manual_override.is_(False), D.parked.is_(False),
                 or_(D.verified_match_status == "review", and_(D.verified_match_status == "unknown", D.last_checked_at.is_not(None)))))

    def mp(r) -> IssueRow:
        return IssueRow(id=r.k, issue_type="unconfirmed_domains", subject_type="domain", subject_id=r.k, organisation_id=r.organisation_id, organisation_name=r.o_name,
                        summary=f"{r.domain} is '{r.verified_match_status}' (confidence {r.confidence:.2f}); an operator decision or a fresh crawl is needed",
                        classification=r.verified_match_status, detected_at=r.last_checked_at or r.updated_at,
                        details={"domain": r.domain, "website_url": r.website_url, "status": r.verified_match_status, "confidence": r.confidence, "evidence_items": r.n_ev})
    return st, D.id, mp


def _q_missing_websites(classification: str | None):
    C = OrganisationCompleteness
    cls = case((C.official_website == "no", "genuinely_unavailable"), (C.official_website == "in_progress", "in_progress"), else_="incomplete_research")
    has_site = exists().where(D.organisation_id == O.id, D.verified_match_status.in_(("confirmed", "high")), D.parked.is_(False))
    st = (select(O.id.label("k"), _nm(O).label("o_name"), O.country_code, O.discovery_source, O.last_checked_at, O.first_discovered_at, cls.label("cls"), C.score)
          .select_from(O).outerjoin(C, C.organisation_id == O.id).where(O.merged_into_id.is_(None), not_(has_site)))
    if classification:
        st = st.where(cls == classification)

    def mp(r) -> IssueRow:
        return IssueRow(id=r.k, issue_type="missing_websites", subject_type="organisation", subject_id=r.k, organisation_id=r.k, organisation_name=r.o_name,
                        summary={"genuinely_unavailable": "Researched: no official website found.", "in_progress": "Website research is queued or running.",
                                 "incomplete_research": "No confirmed website and the research ladder has not finished."}[r.cls],
                        classification=r.cls, detected_at=r.last_checked_at or r.first_discovered_at,
                        details={"country_code": r.country_code, "discovery_source": r.discovery_source, "completeness_score": r.score})
    return st, O.id, mp


def _q_stale_emails(days: int):
    cutoff = datetime.now(UTC) - timedelta(days=days)
    st = (select(E.id.label("k"), E.organisation_id, E.email_normalised, E.origin, E.last_seen_at, E.last_validated_at, E.confidence, _nm(O).label("o_name"))
          .select_from(E).join(O, O.id == E.organisation_id)
          .where(O.merged_into_id.is_(None), E.origin.in_(("published", "corroborated")), E.suppressed_at.is_(None), E.last_seen_at < cutoff))

    def mp(r) -> IssueRow:
        return IssueRow(id=r.k, issue_type="stale_emails", subject_type="email", subject_id=r.k, organisation_id=r.organisation_id, organisation_name=r.o_name,
                        summary=f"Last seen on a page {r.last_seen_at:%Y-%m-%d} (older than {days} days)", detected_at=r.last_seen_at,
                        details={"email": r.email_normalised, "origin": r.origin, "confidence": r.confidence, "last_validated_at": r.last_validated_at})
    return st, E.id, mp


_CONFLICT_SQL = """
SELECT (d.organisation_id * 10 + 1) AS k, 'domain_conflict' AS kind, 'organisation' AS subject_type, d.organisation_id AS subject_id, d.organisation_id AS org_id,
       'organisation has ' || count(DISTINCT d.registrable_domain) || ' different confirmed websites: ' || string_agg(DISTINCT d.registrable_domain, ', ') AS summary,
       jsonb_build_object('domains', jsonb_agg(DISTINCT d.registrable_domain)) AS detail, max(d.updated_at) AS at
  FROM organisation_domains d WHERE d.verified_match_status IN ('confirmed','high') AND NOT d.parked GROUP BY d.organisation_id HAVING count(DISTINCT d.registrable_domain) > 1
UNION ALL
SELECT (l.organisation_id * 10 + 2), 'company_number_conflict', 'organisation', l.organisation_id, l.organisation_id,
       'organisation is linked to different company numbers: ' || string_agg(DISTINCT l.identifier_value, ', '),
       jsonb_build_object('company_numbers', jsonb_agg(DISTINCT l.identifier_value)), max(l.observed_at)
  FROM organisation_record_links l WHERE l.identifier_type = 'company_number' AND l.status = 'linked' GROUP BY l.organisation_id HAVING count(DISTINCT l.identifier_value) > 1
UNION ALL
SELECT (e.id * 10 + 3), 'origin_conflict', 'email', e.id, e.organisation_id,
       CASE WHEN e.origin IN ('pattern_inferred','role_speculative')
            THEN 'address is marked ' || e.origin || ' but was also observed on a page'
            ELSE 'address is marked ' || e.origin || ' but its only evidence is speculative (permutation)' END,
       jsonb_build_object('email', e.email_normalised, 'origin', e.origin), e.updated_at
  FROM email_addresses e WHERE
    (e.origin IN ('pattern_inferred','role_speculative') AND EXISTS (SELECT 1 FROM contact_evidence ce WHERE ce.email_id = e.id AND ce.extraction_method <> 'permutation'))
    OR (e.origin IN ('published','corroborated') AND EXISTS (SELECT 1 FROM contact_evidence ce WHERE ce.email_id = e.id AND ce.extraction_method = 'permutation')
        AND NOT EXISTS (SELECT 1 FROM contact_evidence ce WHERE ce.email_id = e.id AND ce.extraction_method <> 'permutation'))
"""


def _q_conflicts():
    cols = [Column("k", BigInteger), Column("kind", String), Column("subject_type", String), Column("subject_id", BigInteger), Column("org_id", BigInteger),
            Column("summary", String), Column("detail", JSONB), Column("at", String)]
    sub = text(_CONFLICT_SQL).columns(*cols).subquery("c")
    st = (select(sub.c.k, sub.c.kind, sub.c.subject_type, sub.c.subject_id, sub.c.org_id, sub.c.summary, sub.c.detail, sub.c.at, _nm(O).label("o_name"))
          .select_from(sub).join(O, O.id == sub.c.org_id).where(O.merged_into_id.is_(None)))

    def mp(r) -> IssueRow:
        return IssueRow(id=r.k, issue_type="conflicting_evidence", subject_type=r.subject_type, subject_id=r.subject_id, organisation_id=r.org_id,
                        organisation_name=r.o_name, summary=r.summary, classification=r.kind, details=dict(r.detail or {}))
    return st, sub.c.k, mp


def _q_incomplete_contacts():
    has_mail = exists().where(E.contact_id == Contact.id)
    st = (select(Contact.id.label("k"), Contact.organisation_id, Contact.full_name, Contact.job_title, Contact.identity_confidence, Contact.created_at, _nm(O).label("o_name"))
          .select_from(Contact).join(O, O.id == Contact.organisation_id).where(O.merged_into_id.is_(None), not_(has_mail)))

    def mp(r) -> IssueRow:
        return IssueRow(id=r.k, issue_type="incomplete_contacts", subject_type="contact", subject_id=r.k, organisation_id=r.organisation_id, organisation_name=r.o_name,
                        summary=f"Named contact '{r.full_name}' has no e-mail address", detected_at=r.created_at,
                        details={"full_name": r.full_name, "job_title": r.job_title, "identity_confidence": r.identity_confidence})
    return st, Contact.id, mp


def _q_catchall():
    cond = or_(E.catch_all_status.in_(("confirmed", "suspected")), E.recipient_evidence_status == "inconclusive",
               and_(E.last_validated_at.is_not(None), E.overall_reachability == "unknown"))
    st = (select(E.id.label("k"), E.organisation_id, E.email_normalised, E.catch_all_status, E.recipient_evidence_status, E.overall_reachability, E.last_validated_at,
                 _nm(O).label("o_name")).select_from(E).join(O, O.id == E.organisation_id)
          .where(O.merged_into_id.is_(None), E.suppressed_at.is_(None), E.origin.in_(("published", "corroborated")), cond))

    def mp(r) -> IssueRow:
        why = ("catch-all domain" if r.catch_all_status in ("confirmed", "suspected") else "inconclusive recipient evidence" if r.recipient_evidence_status == "inconclusive"
               else "reachability unknown after validation")
        return IssueRow(id=r.k, issue_type="catchall_or_unknown", subject_type="email", subject_id=r.k, organisation_id=r.organisation_id, organisation_name=r.o_name,
                        summary=f"{why}: deliverability cannot be established", classification=why.split(":")[0], detected_at=r.last_validated_at,
                        details={"email": r.email_normalised, "catch_all_status": r.catch_all_status, "recipient_evidence_status": r.recipient_evidence_status,
                                 "overall_reachability": r.overall_reachability})
    return st, E.id, mp


def _builder(issue: str, *, stale_days: int, classification: str | None):
    if issue == "duplicate_companies":
        return _q_links(True)
    if issue == "unresolved_identities":
        return _q_links(False)
    if issue == "unconfirmed_domains":
        return _q_unconfirmed_domains()
    if issue == "missing_websites":
        return _q_missing_websites(classification)
    if issue == "stale_emails":
        return _q_stale_emails(stale_days)
    if issue == "conflicting_evidence":
        return _q_conflicts()
    if issue == "incomplete_contacts":
        return _q_incomplete_contacts()
    return _q_catchall()


# --------------------------------------------------------------------------- routes
@router.get("/summary", response_model=SummaryOut)
async def data_quality_summary(stale_days: int = Query(90, ge=1, le=3650), session: AsyncSession = Depends(get_session)) -> SummaryOut:
    out = []
    for it in ISSUE_TYPES:
        st, key, _mp = _builder(it, stale_days=stale_days, classification=None)
        n, capped = await capped_count(session, st.with_only_columns(key.label("x")), cap=CAP, timeout_ms=3000)
        out.append(IssueCount(issue_type=it, count=n, capped=capped, action_hint=HINTS[it]))
    return SummaryOut(generated_at=datetime.now(UTC), issues=out, total=sum(i.count or 0 for i in out))


@router.get("/issues/{issue_type}", response_model=Page[IssueRow])
async def list_issues(
    issue_type: IssueType, stale_days: int = Query(90, ge=1, le=3650, description="stale_emails only"),
    classification: str | None = Query(None, max_length=40, description="missing_websites only: incomplete_research | genuinely_unavailable | in_progress"),
    direction: str = "desc", limit: int = Query(50, ge=1, le=500), cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[IssueRow]:
    if classification and classification not in ("incomplete_research", "genuinely_unavailable", "in_progress"):
        raise ApiError(422, "invalid_filter", "classification must be incomplete_research, genuinely_unavailable or in_progress.")
    st, key, mp = _builder(issue_type, stale_days=stale_days, classification=classification if issue_type == "missing_websites" else None)
    sorts = {"id": SortSpec(key, "int")}
    st = st.add_columns(key.label("_sv"), key.label("_id"))
    st = apply_keyset(st, sorts, "id", direction, key, cursor, limit)
    rows, nxt = finish_page((await session.execute(st)).all(), "id", direction, limit)
    total = None
    if not cursor:
        base, k2, _ = _builder(issue_type, stale_days=stale_days, classification=classification if issue_type == "missing_websites" else None)
        total, _c = await capped_count(session, base.with_only_columns(k2.label("x")), cap=CAP, timeout_ms=3000)
    return Page[IssueRow](items=[mp(r) for r in rows], next_cursor=nxt, total_estimate=total)


# --------------------------------------------------------------------------- actions
async def _brief_link(session: AsyncSession, ln: OrganisationRecordLink) -> dict[str, Any]:
    return {"link_id": ln.id, "status": ln.status, "organisation_id": ln.organisation_id, "candidate_org_id": ln.candidate_org_id,
            "confirmed_duplicate": bool((ln.detail or {}).get("confirmed_duplicate"))}


async def _act_link(session: AsyncSession, user: User, action: str, rid: int, reason: str) -> tuple[str, dict, dict, str]:
    ln = await session.get(OrganisationRecordLink, rid)
    if ln is None or ln.status != "review":
        return "skipped", {}, {}, "review link not found or already decided"
    before = await _brief_link(session, ln)
    if action == "confirm_duplicate":
        if ln.candidate_org_id is None:
            return "skipped", before, {}, "this link has no candidate organisation to confirm as a duplicate"
        ln.detail = {**(ln.detail or {}), "confirmed_duplicate": True, "confirmed_by": user.id, "confirmed_at": datetime.now(UTC).isoformat(), "reason": reason}
        await session.flush()
        return "ok", before, await _brief_link(session, ln), "marked as a confirmed duplicate; an admin can now merge it"
    if action == "reject_duplicate":
        res = await resolution.resolve_review(session, rid, accept=False, user_id=user.id)
        return "ok", before, {"link_id": rid, **res}, "marked as NOT the same company"
    try:
        res = await resolution.resolve_review(session, rid, accept=True, user_id=user.id)
    except (ValueError, resolution.MergeConflict) as e:
        return "error", before, {}, str(e)[:300]
    return "ok", before, {"link_id": rid, "result": res.get("status"), "detail": {k: v for k, v in res.items() if isinstance(v, int | str)}}, "merged"


async def _act_domain(session: AsyncSession, user: User, action: str, rid: int, reason: str) -> tuple[str, dict, dict, str]:
    d = (await session.execute(select(D).where(D.id == rid).with_for_update())).scalar_one_or_none()
    if d is None:
        return "skipped", {}, {}, "domain not found"
    before = {"domain": d.domain, "status": d.verified_match_status, "confidence": d.confidence, "manual_override": d.manual_override}
    ev = {"type": "manual", "decision": "confirm" if action == "confirm_domain" else "reject", "by_user": user.id, "at": datetime.now(UTC).isoformat(),
          "note": reason[:500], "via": "data_quality"}
    if action == "confirm_domain":
        d.verified_match_status, d.confidence, d.parked = "confirmed", max(d.confidence or 0.0, 1.0), False
    else:
        d.verified_match_status, d.confidence = "mismatch", 0.0
    d.manual_override = True
    d.evidence = [*(d.evidence or []), ev]
    await session.flush()
    return "ok", before, {"domain": d.domain, "status": d.verified_match_status, "confidence": d.confidence, "manual_override": True}, "decision recorded"


async def _act_org(session: AsyncSession, user: User, action: str, rid: int, reason: str) -> tuple[str, dict, dict, str]:
    o = await session.get(Organisation, rid)
    if o is None or o.merged_into_id is not None:
        return "skipped", {}, {}, "business not found or merged into another record"
    if action == "queue_recrawl":
        n = await enqueue_refresh(session, [rid])
        return ("ok" if n else "skipped"), {"last_checked_at": o.last_checked_at}, {"jobs_queued": n}, "refresh queued" if n else "a refresh for this business is already queued"
    comp = await session.get(OrganisationCompleteness, rid)
    before = {"needs_review": bool(comp.needs_review) if comp else None}
    await session.execute(pg_insert(OrganisationCompleteness).values(organisation_id=rid, needs_review=False)
                          .on_conflict_do_update(index_elements=["organisation_id"], set_={"needs_review": False}))
    return "ok", before, {"needs_review": False}, "marked as reviewed"


async def _act_email(session: AsyncSession, user: User, action: str, rid: int, reason: str) -> tuple[str, dict, dict, str]:
    e = await session.get(EmailAddress, rid)
    if e is None:
        return "skipped", {}, {}, "address not found"
    before = {"marketing_status": e.marketing_status, "suppressed": e.suppressed_at is not None, "last_validated_at": e.last_validated_at}
    if action == "queue_revalidate":
        n = await enqueue_verify(session, [rid], "fast", tag="dq")
        return ("ok" if n else "skipped"), before, {"jobs_queued": n}, "re-validation queued" if n else "a re-validation was just queued"
    await suppress(session, kind="email", value=e.email_normalised, reason="manual", note=reason[:500], user_id=user.id)
    await session.refresh(e)
    return "ok", before, {"marketing_status": e.marketing_status, "suppressed": e.suppressed_at is not None}, "suppressed"


@router.post("/actions", response_model=ActionOut)
async def data_quality_actions(body: ActionIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ActionOut:
    """Apply one review action to up to 500 ids. Each id is processed in its own savepoint and audited with before / after / reason."""
    if body.action == "merge" and ROLE_RANK[user.role] < ROLE_RANK["admin"]:
        raise ApiError(403, "forbidden", f"Merging companies requires the 'admin' role; your role is '{user.role}'.")
    subject = SUBJECT_OF_ACTION[body.action]
    handler = {"review_link": _act_link, "domain": _act_domain, "organisation": _act_org, "email": _act_email}[subject]
    results: list[ActionResult] = []
    for rid in dict.fromkeys(body.ids):
        try:
            async with session.begin_nested():
                status, before, after, msg = await handler(session, user, body.action, rid, body.reason)
                if status == "error":
                    raise _Rollback(msg, before)
        except _Rollback as e:
            results.append(ActionResult(id=rid, status="error", message=e.message, before=e.before))
            continue
        except Exception as e:  # one bad id must not abort the batch
            log.warning("data-quality %s failed for %s", body.action, rid, exc_info=True)
            results.append(ActionResult(id=rid, status="error", message=f"{type(e).__name__}: {str(e)[:200]}"))
            continue
        results.append(ActionResult(id=rid, status=status, message=msg, before=before, after=after))
        if status == "ok":
            await audit(session, user, f"data_quality.{body.action}", subject, rid, {"before": before, "after": after, "reason": body.reason})
    await session.commit()
    ok = sum(1 for r in results if r.status == "ok")
    return ActionOut(action=body.action, subject_type=subject, requested=len(results), ok=ok, skipped=sum(1 for r in results if r.status == "skipped"),
                     errors=sum(1 for r in results if r.status == "error"), results=results)


class _Rollback(Exception):
    def __init__(self, message: str, before: dict):
        super().__init__(message)
        self.message, self.before = message, before
