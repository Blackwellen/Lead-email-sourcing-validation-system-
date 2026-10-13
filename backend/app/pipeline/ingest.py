"""Persist crawl observations: e-mail addresses, contacts and per-page provenance.

Idempotent by construction: every write is an upsert keyed on a database unique constraint
(``uq_email_normalised``, ``uq_evidence_identity``, ``uq_contact_org_identity``,
``uq_contact_evidence``), so re-crawling only refreshes ``last_seen_at`` / ``observed_at`` and adds
evidence rows for genuinely new page content. The caller owns the transaction (we only flush).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..crawler.extract import clean_email, clean_ws, looks_like_person_name
from ..crawler.types import EmailObservation
from ..crawler.urls import registrable_domain, to_ascii_host
from ..models import (
    Contact,
    ContactEvidence,
    EmailAddress,
    EvidenceSource,
    OrganisationDomain,
)

PARSER_VERSION = "crawler-1"
_BASE_RELIABILITY = {"jsonld": 0.9, "vcard": 0.9, "mailto": 0.9, "table": 0.85, "text": 0.8, "obfuscated": 0.75, "pdf": 0.7}

# --------------------------------------------------------------------------- role / free-provider classification
_FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it",
    "ymail.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de", "outlook.com", "outlook.fr",
    "live.com", "live.co.uk", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "aol.co.uk", "proton.me",
    "protonmail.com", "pm.me", "gmx.com", "gmx.de", "gmx.net", "gmx.co.uk", "web.de", "t-online.de", "yandex.com",
    "yandex.ru", "mail.ru", "mail.com", "btinternet.com", "btopenworld.com", "sky.com", "talktalk.net",
    "virginmedia.com", "ntlworld.com", "blueyonder.co.uk", "tiscali.co.uk", "orange.fr", "wanadoo.fr", "free.fr",
    "laposte.net", "libero.it", "qq.com", "163.com", "126.com", "zoho.com", "fastmail.com", "tutanota.com",
    "hey.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "rediffmail.com",
}
_ROLE_MAP: dict[str, str] = {}
for _role, _names in {
    "generic": "info information contact hello hi enquiries enquiry enquire office mail email general admin administrator "
               "reception post team mailbox hallo kontakt contacto contatto bonjour",
    "sales": "sales sale newbusiness business bookings booking orders order lettings reservations quotes quote",
    "support": "support help helpdesk customerservice customercare service servicedesk care techsupport",
    "finance": "accounts account accounting billing finance invoices invoice payments payroll creditcontrol ar ap",
    "hr": "hr jobs careers recruitment recruiting people talent hiring vacancies",
    "marketing": "marketing press media pr news communications comms advertising",
    "legal": "legal compliance privacy dpo gdpr complaints data",
    "technical": "webmaster postmaster hostmaster abuse security tech it sysadmin",
    "no_reply": "noreply no-reply donotreply do-not-reply mailer-daemon bounce bounces unsubscribe",
}.items():
    for _n in _names.split():
        _ROLE_MAP[_n] = _role


def _builtin_role(local: str) -> str | None:
    low = re.sub(r"[^a-z0-9\-]", "", local.lower())
    if low in _ROLE_MAP:
        return _ROLE_MAP[low]
    low2 = low.replace("-", "")
    return _ROLE_MAP.get(low2)


try:  # the verifier package may provide richer lists; use them when present
    from ..verify.lists import classify_role as _ext_classify_role  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - depends on a sibling module
    _ext_classify_role = None
try:
    from ..verify.lists import is_free_provider as _ext_is_free  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    _ext_is_free = None


def classify_role(local_part: str) -> str | None:
    """Role-mailbox type for a local part (None for personal-looking addresses)."""
    if _ext_classify_role is not None:
        try:
            r = _ext_classify_role(local_part)
            if isinstance(r, str) or r is None:
                return r or None
            for attr in ("role_type", "role", "name"):
                v = getattr(r, attr, None)
                if isinstance(v, str):
                    return v
        except Exception:
            pass
    return _builtin_role(local_part)


def is_free_provider(domain: str) -> bool:
    d = domain.lower()
    if _ext_is_free is not None:
        try:
            return bool(_ext_is_free(d))
        except Exception:
            pass
    return d in _FREE_PROVIDERS


# --------------------------------------------------------------------------- name parsing
_PREFIXES = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "sir", "dame", "rev", "reverend", "lord", "lady",
             "herr", "frau", "mme", "monsieur", "madame", "mlle", "sra", "sr", "hr", "fr", "eng", "cllr", "capt", "major", "col"}
_SUFFIX_WORDS = {"jr", "sr", "ii", "iii", "iv", "phd", "mba", "esq", "obe", "mbe", "cbe", "kc", "qc", "frics", "mrics",
                 "acca", "fca", "aca", "cfa", "llb", "llm", "bsc", "msc", "ba", "ma", "mphil", "cima", "fcca", "cpa",
                 "dds", "md", "rn", "frcs", "fcii", "chfc", "cfp", "arb", "ciarb"}
_PARTICLES = {"van", "von", "de", "del", "della", "di", "da", "dos", "das", "du", "le", "la", "el", "al", "bin", "ibn",
              "ter", "ten", "der", "den", "st", "op", "zu", "af", "ap", "ben", "bint", "y", "e", "mac"}
_POSTNOMINAL = re.compile(r"^[A-Z]{2,6}$")


@dataclass(frozen=True, slots=True)
class ParsedName:
    full_name: str  # original (cleaned) text as published
    given_name: str | None
    family_name: str | None
    prefix: str | None = None
    suffix: str | None = None
    identity_key: str = ""


def _letters_key(tokens: Iterable[str]) -> str:
    s = " ".join(tokens)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", s.casefold().replace("ß", "ss")).strip()


def parse_name(raw: str | None) -> ParsedName | None:
    """Conservative name parser: honorifics, suffixes, particles, compound surnames, unicode.

    Returns None unless the text looks like a real person's name (>= 2 name tokens). The original
    text is preserved in ``full_name``; given/family are best-effort.
    """
    if not raw:
        return None
    original = clean_ws(unicodedata.normalize("NFC", raw))
    s = re.sub(r"\([^)]*\)", " ", original)
    s = clean_ws(s).strip(",;:|-–— ")
    if not s:
        return None
    # "Family, Given" ordering (single comma, right side is not a suffix)
    if s.count(",") == 1:
        left, right = (p.strip() for p in s.split(","))
        if right and right.lower().rstrip(".") not in _SUFFIX_WORDS and not _POSTNOMINAL.match(right) and len(left.split()) <= 3:
            s = f"{right} {left}"
    s = s.replace(",", " ")
    toks = s.split()
    prefix = None
    while toks and toks[0].lower().rstrip(".") in _PREFIXES:
        prefix = prefix or toks[0].rstrip(".")
        toks.pop(0)
    suffix_parts: list[str] = []
    while toks and (toks[-1].lower().rstrip(".") in _SUFFIX_WORDS or (len(toks) > 2 and _POSTNOMINAL.match(toks[-1]))):
        suffix_parts.insert(0, toks.pop())
    if len(toks) < 2:
        return None
    if not looks_like_person_name(" ".join(toks)):
        return None
    # family name = trailing particle-led group, else last token
    fam_start = len(toks) - 1
    for i in range(1, len(toks) - 1):
        if toks[i].lower().rstrip(".") in _PARTICLES:
            fam_start = i
            break
    given = " ".join(toks[:fam_start]) or None
    family = " ".join(toks[fam_start:]) or None
    full = " ".join(([prefix] if prefix else []) + toks + suffix_parts)
    return ParsedName(
        full_name=clean_ws(full)[:300], given_name=given[:150] if given else None,
        family_name=family[:150] if family else None, prefix=prefix, suffix=" ".join(suffix_parts) or None,
        identity_key="name:" + _letters_key(toks),
    )


# --------------------------------------------------------------------------- helpers
def normalise_email(value: str) -> str | None:
    """Canonical comparison form: validated, lower-case, IDN domain as punycode."""
    clean = clean_email(value) if value else None
    if not clean:
        return None
    local, _, domain = clean.rpartition("@")
    d = to_ascii_host(domain)
    if not d:
        return None
    return f"{local.lower()}@{d}"


def _hash_group(items: Sequence[tuple[str, EmailObservation]]) -> str:
    lines = sorted(f"{e}|{o.name or ''}|{o.title or ''}|{o.department or ''}" for e, o in items)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _page_reg(url: str) -> str | None:
    try:
        return registrable_domain(urlsplit(url).hostname or "")
    except ValueError:
        return None


_ORIGIN_RANK = {"role_speculative": 0, "pattern_inferred": 1, "published": 2, "corroborated": 3}

# --------------------------------------------------------------------------- "live" evidence
# Evidence that the address is published TODAY: a current (non-archived) observation that is not a speculative permutation.
# Common Crawl archive rows (``historical``) and permutation guesses (``evidence_type='permutation'``) never count as live.
NON_LIVE_EVIDENCE_TYPES = ("permutation",)


def live_evidence_sql(email_col: str = "e.id") -> str:
    """SQL ``EXISTS`` fragment: the address has at least one live (non-historical, non-permutation) evidence row."""
    return (f"EXISTS (SELECT 1 FROM contact_evidence lce JOIN evidence_sources les ON les.id = lce.evidence_id "
            f"WHERE lce.email_id = {email_col} AND NOT les.historical AND les.evidence_type <> 'permutation')")


def nonlive_evidence_sql(email_col: str = "e.id") -> str:
    """SQL ``EXISTS`` fragment: the address has archive-only or speculative (permutation) evidence."""
    return (f"EXISTS (SELECT 1 FROM contact_evidence nce JOIN evidence_sources nes ON nes.id = nce.evidence_id "
            f"WHERE nce.email_id = {email_col} AND (nes.historical OR nes.evidence_type = 'permutation'))")


def live_evidence_exists(email_id_col):
    """SQLAlchemy ``EXISTS`` twin of :func:`live_evidence_sql`."""
    from sqlalchemy import exists

    return exists().where(ContactEvidence.email_id == email_id_col, ContactEvidence.evidence_id == EvidenceSource.id,
                          EvidenceSource.historical.is_(False), EvidenceSource.evidence_type.not_in(NON_LIVE_EVIDENCE_TYPES))


def nonlive_evidence_exists(email_id_col):
    from sqlalchemy import exists, or_

    return exists().where(ContactEvidence.email_id == email_id_col, ContactEvidence.evidence_id == EvidenceSource.id,
                          or_(EvidenceSource.historical.is_(True), EvidenceSource.evidence_type.in_(NON_LIVE_EVIDENCE_TYPES)))


async def persist_observations(
    session: AsyncSession,
    org_id: int,
    domain_row: OrganisationDomain | None,
    observations: Iterable[EmailObservation],
    *,
    run_id: int | None = None,
    source_adapter: str = "website_crawl",
    licence: str | None = None,
    now: datetime | None = None,
    archived_at: datetime | None = None,
) -> list[tuple[int, bool]]:
    """Upsert addresses, contacts and evidence. Returns ``[(email_id, created), ...]`` (one per address).

    ``archived_at`` marks the observations as ARCHIVED (e.g. Common Crawl capture time): a new address gets
    ``first_seen_at = last_seen_at = archived_at``; an existing address is never upgraded in origin (an archive cannot
    turn a speculative guess into a published address), its ``last_seen_at`` is not bumped and existing evidence context
    (e.g. a live confirmation) is kept.

    ``contact_evidence.run_id`` keeps the run that FIRST recorded that (address, evidence, method); later runs that re-observe
    it are attributed through ``run_yield`` instead (see ``exports.filter_conditions``)."""
    now = now or datetime.now(UTC)
    archive = archived_at is not None
    # ---- the organisation's own domains
    own_regs: set[str] = set()
    if domain_row is not None and domain_row.registrable_domain:
        own_regs.add(domain_row.registrable_domain)
    rows = (await session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == org_id))).scalars().all()
    for r in rows:
        if r.manual_override or r.verified_match_status in ("confirmed", "high"):
            own_regs.add(r.registrable_domain)

    # ---- normalise + expand (email, method) pairs
    by_email: dict[str, list[EmailObservation]] = {}
    order: list[str] = []
    norm_of: dict[int, str] = {}
    for ob in observations:
        ne = normalise_email(ob.email)
        if not ne:
            continue
        norm_of[id(ob)] = ne
        if ne not in by_email:
            by_email[ne] = []
            order.append(ne)
        by_email[ne].append(ob)
    if not by_email:
        return []

    groups: dict[tuple[str, str], list[tuple[str, EmailObservation]]] = defaultdict(list)
    for ne, obs in by_email.items():
        for ob in obs:
            for method in dict.fromkeys((ob.method, *ob.also_methods)):
                groups[(ob.page_url, method)].append((ne, ob))

    # ---- existing corroboration (distinct source URLs already on file per address)
    existing_rows = {
        r.email_normalised: r for r in (await session.execute(
            select(EmailAddress).where(EmailAddress.email_normalised.in_(order)))).scalars()
    }
    prior_urls: dict[str, set[str]] = defaultdict(set)
    if existing_rows:
        q = (select(EmailAddress.email_normalised, EvidenceSource.source_url)
             .join(ContactEvidence, ContactEvidence.email_id == EmailAddress.id)
             .join(EvidenceSource, EvidenceSource.id == ContactEvidence.evidence_id)
             .where(EmailAddress.email_normalised.in_(list(existing_rows))))
        for em, url in (await session.execute(q)).all():
            if url:
                prior_urls[em].add(url)

    # ---- evidence rows (one per page+method group)
    evidence_id: dict[tuple[str, str], int] = {}
    for (page_url, method), items in groups.items():
        page_own = _page_reg(page_url) in own_regs
        rels = []
        for ne, _ob in items:
            ereg = registrable_domain(ne) or ""
            base = _BASE_RELIABILITY.get(method, 0.6)
            if ereg in own_regs:
                f = 1.0
            elif is_free_provider(ne.rsplit("@", 1)[1]):
                f = 0.6
            else:
                f = 0.5
            rels.append(base * f * (1.0 if page_own else 0.7))
        rel = round(sum(rels) / len(rels), 3)
        path = urlsplit(page_url).path
        vals = {
            "source_adapter": source_adapter, "source_url": page_url, "source_document": (path.rsplit("/", 1)[-1][:300] or None) if method == "pdf" else None,
            "licence": licence, "evidence_type": method, "observed_at": now, "parser_version": PARSER_VERSION,
            "content_hash": _hash_group(items), "source_reliability": rel,
        }
        stmt = pg_insert(EvidenceSource).values(**vals)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_evidence_identity",
            set_={"observed_at": now, "source_reliability": rel, "licence": func.coalesce(stmt.excluded.licence, EvidenceSource.licence)},
        ).returning(EvidenceSource.id)
        evidence_id[(page_url, method)] = (await session.execute(stmt)).scalar_one()

    results: list[tuple[int, bool]] = []
    for ne in order:
        obs = by_email[ne]
        local, _, dom = ne.rpartition("@")
        ereg = registrable_domain(dom) or dom
        email_own = ereg in own_regs
        pages = {o.page_url for o in obs}
        any_own_page = any(_page_reg(p) in own_regs for p in pages)
        corroborating = pages | prior_urls.get(ne, set())
        if email_own:
            origin, needs_review = "published", False
        elif len(corroborating) >= 2:
            origin, needs_review = "corroborated", False
        elif any_own_page and is_free_provider(dom):
            origin, needs_review = "published", False  # published by the organisation itself on its own page
        else:
            origin, needs_review = "published", True  # third-party domain, single source: flagged, low reliability
        if archive:  # archived pages never corroborate each other; an archive-only address always needs review
            origin, needs_review = "published", True
        role = classify_role(local)
        free = is_free_provider(dom)
        primary = sorted(obs, key=lambda o: -_BASE_RELIABILITY.get(o.method, 0.5))[0]
        original = primary.email.strip()

        # ---- contact (only when a name is published next to the address; never for shared role mailboxes)
        contact_id: int | None = None
        parsed = None
        if role is None:
            for o in sorted(obs, key=lambda o: -_BASE_RELIABILITY.get(o.method, 0.5)):
                parsed = parse_name(o.name)
                if parsed:
                    src = o
                    break
        if parsed is not None:
            conf = 0.55 + (0.2 if src.method in ("jsonld", "vcard", "table") else 0.0) + (0.05 if src.title else 0.0)
            cvals = {
                "organisation_id": org_id, "full_name": parsed.full_name, "given_name": parsed.given_name,
                "family_name": parsed.family_name, "job_title": (src.title or None) and src.title[:300],
                "department": (src.department or None) and src.department[:200], "identity_confidence": min(conf, 0.85),
                "identity_key": parsed.identity_key,
            }
            cstmt = pg_insert(Contact).values(**cvals)
            cstmt = cstmt.on_conflict_do_update(
                constraint="uq_contact_org_identity",
                set_={
                    "job_title": func.coalesce(Contact.job_title, cstmt.excluded.job_title),
                    "department": func.coalesce(Contact.department, cstmt.excluded.department),
                    "identity_confidence": func.greatest(Contact.identity_confidence, cstmt.excluded.identity_confidence),
                },
            ).returning(Contact.id)
            contact_id = (await session.execute(cstmt)).scalar_one()

        # ---- e-mail address
        vals = {
            "organisation_id": org_id, "contact_id": contact_id, "email_original": original[:320], "email_normalised": ne,
            "local_part": local[:200], "domain": dom[:255], "registrable_domain": ereg[:255],
            "source_type": primary.method, "origin": origin, "role_type": role, "is_free_provider": free,
            "needs_review": needs_review, "first_seen_at": archived_at or now, "last_seen_at": archived_at or now,
        }
        istmt = pg_insert(EmailAddress).values(**vals).on_conflict_do_nothing(index_elements=["email_normalised"]).returning(EmailAddress.id)
        new_id = (await session.execute(istmt)).scalar_one_or_none()
        if new_id is not None:
            email_id, created = new_id, True
        else:
            created = False
            ex = existing_rows.get(ne)
            if ex is None:
                ex = (await session.execute(select(EmailAddress).where(EmailAddress.email_normalised == ne))).scalar_one()
            email_id = ex.id
            upd: dict = {"first_seen_at": func.least(EmailAddress.first_seen_at, archived_at)} if archive else {"last_seen_at": now}
            if not archive and _ORIGIN_RANK.get(origin, 0) > _ORIGIN_RANK.get(ex.origin, 0):
                upd["origin"] = origin
                if origin == "corroborated":
                    upd["needs_review"] = False
            if ex.contact_id is None and contact_id is not None:
                upd["contact_id"] = contact_id
            if ex.role_type is None and role is not None:
                upd["role_type"] = role
            await session.execute(update(EmailAddress).where(EmailAddress.id == email_id).values(**upd))
        results.append((email_id, created))

        # ---- per-observation evidence links
        for o in obs:
            for method in dict.fromkeys((o.method, *o.also_methods)):
                ev_id = evidence_id[(o.page_url, method)]
                ctx = {k: v for k, v in {
                    "name": o.name, "title": o.title, "department": o.department, "phone": o.phone,
                    "snippet": (o.context or "")[:300] or None, "page_url": o.page_url,
                    "as_published": o.email.strip(),
                }.items() if v}
                cev = pg_insert(ContactEvidence).values(
                    email_id=email_id, contact_id=contact_id, organisation_id=org_id, evidence_id=ev_id, run_id=run_id,
                    extraction_method=method, context=ctx, observed_at=now)
                cev = cev.on_conflict_do_update(
                    constraint="uq_contact_evidence",
                    set_={
                        "observed_at": now,
                        # archive re-observation: existing keys (unconfirmed / confirmed_live_at ...) win over the new context
                        "context": cev.excluded.context.op("||")(func.coalesce(ContactEvidence.context, cev.excluded.context)) if archive else ctx,
                        "run_id": func.coalesce(ContactEvidence.run_id, cev.excluded.run_id),
                        "contact_id": func.coalesce(ContactEvidence.contact_id, cev.excluded.contact_id),
                    })
                await session.execute(cev)
    if domain_row is not None:
        domain_row.last_crawled_at = now
    await session.flush()
    return results
