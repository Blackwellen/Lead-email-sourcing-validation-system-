"""Data-driven business taxonomy: Category -> Subcategory -> Specialism, each with keyword packs.

Nothing here is specific to property: the seed (``app/data/taxonomy_seed.json``) ships Property, Events, General
business services and General marketplace services, but every operation works on arbitrary rows in ``categories``.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category, Organisation, SourcingRun

SEED_PATH = Path(__file__).resolve().parent / "data" / "taxonomy_seed.json"
LEVELS = ("category", "subcategory", "specialism")
LIST_FIELDS = ("keywords", "synonyms", "excluded_keywords", "sic_codes", "osm_tags", "default_sources")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,118}$")
_SIC_RE = re.compile(r"^\d{4,5}$")
_OSM_RE = re.compile(r"^[a-z_:0-9]+(=[^=\s;]+|=\*)?$")
MAX_LIST = 500
MAX_TERM_LEN = 120
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_NODES = 5000


class TaxonomyError(ValueError):
    pass


@dataclass
class SeedResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass
class DiscoveryQueryTerms:
    category_ids: list[int] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    sic_codes: list[str] = field(default_factory=list)
    osm_tags: list[str] = field(default_factory=list)
    default_sources: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- validation helpers
def _clean_list(name: str, values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[|;]", values)]
    if not isinstance(values, (list, tuple)):
        raise TaxonomyError(f"{name} must be a list")
    out: list[str] = []
    for v in values:
        s = re.sub(r"\s+", " ", str(v)).strip()
        if not s:
            continue
        if len(s) > MAX_TERM_LEN:
            raise TaxonomyError(f"{name}: term too long")
        if name == "sic_codes" and not _SIC_RE.match(s):
            raise TaxonomyError(f"invalid SIC code {s!r}")
        if name == "osm_tags" and not _OSM_RE.match(s):
            raise TaxonomyError(f"invalid OSM tag {s!r}")
        if name in ("keywords", "synonyms", "excluded_keywords"):
            s = s.lower()
        if s not in out:
            out.append(s)
    if len(out) > MAX_LIST:
        raise TaxonomyError(f"{name}: too many entries")
    return out


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


def _check_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise TaxonomyError(f"invalid slug {slug!r}")
    return slug


def _level_for_parent(parent: Category | None) -> str:
    if parent is None:
        return "category"
    idx = LEVELS.index(parent.level) if parent.level in LEVELS else 0
    if idx >= len(LEVELS) - 1:
        raise TaxonomyError("a specialism cannot have children")
    return LEVELS[idx + 1]


# --------------------------------------------------------------------------- seed / import / export
def load_seed(path: Path | None = None) -> dict[str, Any]:
    with open(path or SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _flatten(nodes: list[dict[str, Any]], parent_slug: str | None = None) -> list[dict[str, Any]]:
    out = []
    for n in nodes:
        flat = {k: v for k, v in n.items() if k != "children"}
        flat.setdefault("parent_slug", parent_slug)
        if flat.get("parent_slug") is None:
            flat["parent_slug"] = parent_slug
        out.append(flat)
        out.extend(_flatten(n.get("children", []), n["slug"]))
    return out


async def _upsert_nodes(session: AsyncSession, flat: list[dict[str, Any]], *, overwrite: bool) -> SeedResult:
    """``overwrite=False`` (seed): never clobber operator edits -- union list fields, keep name/enabled/merge state.
    ``overwrite=True`` (import): provided fields replace stored ones."""
    res = SeedResult()
    existing = {c.slug: c for c in (await session.execute(select(Category))).scalars()}
    for n in flat:  # parents precede children in tree order; CSV imports are re-sorted by caller
        slug = _check_slug(n["slug"])
        parent_slug = n.get("parent_slug")
        parent = existing.get(parent_slug) if parent_slug else None
        if parent_slug and parent is None:
            raise TaxonomyError(f"unknown parent {parent_slug!r} for {slug!r}")
        lists = {f: _clean_list(f, n.get(f)) for f in LIST_FIELDS}
        name = (n.get("name") or "").strip()[:200]
        if not name:
            raise TaxonomyError(f"{slug}: name required")
        level = n.get("level") or _level_for_parent(parent)
        if level not in LEVELS:
            raise TaxonomyError(f"{slug}: bad level {level!r}")
        row = existing.get(slug)
        if row is None:
            row = Category(slug=slug, name=name, level=level, parent_id=parent.id if parent else None,
                           enabled=bool(n.get("enabled", True)), **lists)
            session.add(row)
            await session.flush()
            existing[slug] = row
            res.created += 1
            continue
        changed = False
        if overwrite:
            if row.name != name:
                row.name, changed = name, True
            for f in LIST_FIELDS:
                if f in n and getattr(row, f) != lists[f]:
                    setattr(row, f, lists[f])
                    changed = True
            if "enabled" in n and row.enabled != bool(n["enabled"]):
                row.enabled, changed = bool(n["enabled"]), True
            new_parent = parent.id if parent else None
            if "parent_slug" in n and row.parent_id != new_parent:
                await _assert_no_cycle(session, row.id, new_parent)
                row.parent_id, changed = new_parent, True
            if row.level != level:
                row.level, changed = level, True
        else:
            for f in LIST_FIELDS:
                merged = _union(getattr(row, f) or [], lists[f])
                if merged != (getattr(row, f) or []):
                    setattr(row, f, merged)
                    changed = True
            if row.parent_id is None and parent is not None and row.merged_into_id is None:
                row.parent_id, changed = parent.id, True
        res.updated += int(changed)
        res.unchanged += int(not changed)
    await session.flush()
    return res


async def seed_taxonomy(session: AsyncSession, seed: dict[str, Any] | None = None) -> SeedResult:
    """Idempotent: creates missing seed nodes; for existing ones only unions keyword lists (operator edits survive)."""
    data = seed or load_seed()
    return await _upsert_nodes(session, _flatten(data["categories"]), overwrite=False)


def _parse_csv(raw: str) -> list[dict[str, Any]]:
    rd = csv.DictReader(io.StringIO(raw))
    rows = []
    for r in rd:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if not row.get("slug"):
            continue
        row["parent_slug"] = row.get("parent_slug") or None
        rows.append(row)
        if len(rows) > MAX_IMPORT_NODES:
            raise TaxonomyError("too many rows")
    # parents first
    by_slug = {r["slug"]: r for r in rows}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(r: dict[str, Any], stack: tuple[str, ...] = ()) -> None:
        if r["slug"] in seen:
            return
        if r["slug"] in stack:
            raise TaxonomyError("cycle in parent_slug")
        p = r.get("parent_slug")
        if p and p in by_slug:
            visit(by_slug[p], (*stack, r["slug"]))
        seen.add(r["slug"])
        ordered.append(r)

    for r in rows:
        visit(r)
    return ordered


async def import_taxonomy(session: AsyncSession, payload: str | bytes | dict | list, fmt: str = "json") -> SeedResult:
    """Import JSON (tree like the seed / ``export_taxonomy``, or flat list with ``parent_slug``) or CSV
    (columns: slug,name,parent_slug,level,keywords,synonyms,excluded_keywords,sic_codes,osm_tags,default_sources;
    list cells separated by ``|`` or ``;``).  Provided fields overwrite stored values (operator-driven import)."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_IMPORT_BYTES:
            raise TaxonomyError("import too large")
        payload = payload.decode("utf-8-sig", errors="replace")
    if isinstance(payload, str) and len(payload) > MAX_IMPORT_BYTES:
        raise TaxonomyError("import too large")
    if fmt == "csv":
        assert isinstance(payload, str)
        flat = _parse_csv(payload)
    else:
        data = json.loads(payload) if isinstance(payload, str) else payload
        nodes = data["categories"] if isinstance(data, dict) else data
        if any("children" in n for n in nodes):
            flat = _flatten(nodes)
        else:
            flat = list(nodes)
            by = {n["slug"]: n for n in flat}
            ordered: list[dict[str, Any]] = []
            seen: set[str] = set()

            def visit(n: dict[str, Any], depth: int = 0) -> None:
                if n["slug"] in seen:
                    return
                if depth > 10:
                    raise TaxonomyError("cycle in parent_slug")
                p = n.get("parent_slug")
                if p and p in by:
                    visit(by[p], depth + 1)
                seen.add(n["slug"])
                ordered.append(n)

            for n in flat:
                visit(n)
            flat = ordered
    if len(flat) > MAX_IMPORT_NODES:
        raise TaxonomyError("too many nodes")
    return await _upsert_nodes(session, flat, overwrite=True)


async def export_taxonomy(session: AsyncSession, *, include_disabled: bool = True) -> dict[str, Any]:
    rows = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
    by_id = {c.id: c for c in rows}
    children: dict[int | None, list[Category]] = {}
    for c in rows:
        if not include_disabled and not c.enabled:
            continue
        children.setdefault(c.parent_id if c.parent_id in by_id else None, []).append(c)

    def node(c: Category) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slug": c.slug, "name": c.name, "level": c.level, "enabled": c.enabled,
            **{f: list(getattr(c, f) or []) for f in LIST_FIELDS},
            "merged_into": by_id[c.merged_into_id].slug if c.merged_into_id in by_id else None,
        }
        d["children"] = [node(x) for x in children.get(c.id, [])]
        return d

    return {"version": 1, "categories": [node(c) for c in children.get(None, [])]}


# --------------------------------------------------------------------------- editing services
async def _get(session: AsyncSession, category_id: int) -> Category:
    c = await session.get(Category, category_id)
    if c is None:
        raise TaxonomyError(f"category {category_id} not found")
    return c


async def descendant_ids(session: AsyncSession, ids: list[int], *, include_self: bool = True) -> list[int]:
    if not ids:
        return []
    q = text(
        """WITH RECURSIVE t AS (
             SELECT id FROM categories WHERE id = ANY(:ids)
             UNION
             SELECT c.id FROM categories c JOIN t ON c.parent_id = t.id)
           SELECT id FROM t ORDER BY id"""
    )
    got = [r[0] for r in (await session.execute(q, {"ids": list(ids)})).all()]
    return got if include_self else [i for i in got if i not in set(ids)]


async def _assert_no_cycle(session: AsyncSession, category_id: int, new_parent_id: int | None) -> None:
    if new_parent_id is None:
        return
    if new_parent_id == category_id or new_parent_id in await descendant_ids(session, [category_id]):
        raise TaxonomyError("parent change would create a cycle")


async def create_category(
    session: AsyncSession, *, slug: str, name: str, parent_id: int | None = None, keywords: list[str] | None = None,
    synonyms: list[str] | None = None, excluded_keywords: list[str] | None = None, sic_codes: list[str] | None = None,
    osm_tags: list[str] | None = None, default_sources: list[str] | None = None,
) -> Category:
    slug = _check_slug(slug)
    if (await session.execute(select(Category.id).where(Category.slug == slug))).first():
        raise TaxonomyError(f"slug {slug!r} already exists")
    parent = await _get(session, parent_id) if parent_id else None
    c = Category(
        slug=slug, name=name.strip()[:200], level=_level_for_parent(parent), parent_id=parent_id,
        keywords=_clean_list("keywords", keywords), synonyms=_clean_list("synonyms", synonyms),
        excluded_keywords=_clean_list("excluded_keywords", excluded_keywords), sic_codes=_clean_list("sic_codes", sic_codes),
        osm_tags=_clean_list("osm_tags", osm_tags), default_sources=_clean_list("default_sources", default_sources),
    )
    if not c.name:
        raise TaxonomyError("name required")
    session.add(c)
    await session.flush()
    return c


async def edit_category(session: AsyncSession, category_id: int, **fields: Any) -> Category:
    c = await _get(session, category_id)
    allowed = {"name", "slug", "parent_id", "enabled", *LIST_FIELDS}
    bad = set(fields) - allowed
    if bad:
        raise TaxonomyError(f"cannot edit {sorted(bad)}")
    if "slug" in fields:
        new = _check_slug(fields["slug"])
        if new != c.slug and (await session.execute(select(Category.id).where(Category.slug == new))).first():
            raise TaxonomyError(f"slug {new!r} already exists")
        c.slug = new
    if "name" in fields:
        if not str(fields["name"]).strip():
            raise TaxonomyError("name required")
        c.name = str(fields["name"]).strip()[:200]
    if "parent_id" in fields:
        pid = fields["parent_id"]
        await _assert_no_cycle(session, c.id, pid)
        parent = await _get(session, pid) if pid else None
        c.parent_id = pid
        c.level = _level_for_parent(parent)
    if "enabled" in fields:
        c.enabled = bool(fields["enabled"])
    for f in LIST_FIELDS:
        if f in fields:
            setattr(c, f, _clean_list(f, fields[f]))
    await session.flush()
    return c


async def add_synonyms(session: AsyncSession, category_id: int, synonyms: list[str]) -> Category:
    c = await _get(session, category_id)
    c.synonyms = _union(c.synonyms or [], _clean_list("synonyms", synonyms))
    await session.flush()
    return c


async def disable_category(session: AsyncSession, category_id: int, *, include_descendants: bool = True) -> int:
    ids = await descendant_ids(session, [category_id]) if include_descendants else [category_id]
    await _get(session, category_id)
    await session.execute(update(Category).where(Category.id.in_(ids)).values(enabled=False))
    await session.flush()
    return len(ids)


async def enable_category(session: AsyncSession, category_id: int, *, include_descendants: bool = False) -> int:
    ids = await descendant_ids(session, [category_id]) if include_descendants else [category_id]
    await session.execute(update(Category).where(Category.id.in_(ids), Category.merged_into_id.is_(None)).values(enabled=True))
    await session.flush()
    return len(ids)


async def merge_categories(session: AsyncSession, source_id: int, target_id: int, *, merge_terms: bool = True) -> dict[str, int]:
    """Merge ``source`` into ``target``: organisations, runs and child categories are re-pointed, the source is
    disabled with ``merged_into_id`` set, and (optionally) its keyword packs are unioned into the target."""
    if source_id == target_id:
        raise TaxonomyError("cannot merge a category into itself")
    src, tgt = await _get(session, source_id), await _get(session, target_id)
    if src.merged_into_id is not None:
        raise TaxonomyError("source is already merged")
    if tgt.merged_into_id is not None:
        raise TaxonomyError("target is itself merged; merge into its current target")
    if target_id in await descendant_ids(session, [source_id]):
        raise TaxonomyError("cannot merge a category into its own descendant")
    orgs = await session.execute(update(Organisation).where(Organisation.category_id == source_id).values(category_id=target_id))
    r1 = await session.execute(update(SourcingRun).where(SourcingRun.category_id == source_id).values(category_id=target_id))
    r2 = await session.execute(update(SourcingRun).where(SourcingRun.destination_category_id == source_id).values(destination_category_id=target_id))
    kids = await session.execute(update(Category).where(Category.parent_id == source_id).values(parent_id=target_id))
    # anything previously merged into the source now points at the final target (no chains)
    await session.execute(update(Category).where(Category.merged_into_id == source_id).values(merged_into_id=target_id))
    if merge_terms:
        for f in LIST_FIELDS:
            setattr(tgt, f, _union(getattr(tgt, f) or [], getattr(src, f) or []))
        tgt.synonyms = _union(tgt.synonyms, [src.name.lower()])
    src.merged_into_id, src.enabled, src.parent_id = target_id, False, None
    await session.flush()
    return {
        "organisations": orgs.rowcount or 0, "runs": (r1.rowcount or 0) + (r2.rowcount or 0), "children": kids.rowcount or 0,
    }


async def resolve_merged(session: AsyncSession, category_id: int) -> int:
    """Follow ``merged_into_id`` to the live category id."""
    seen = set()
    cur = category_id
    while cur not in seen:
        seen.add(cur)
        c = await session.get(Category, cur)
        if c is None or c.merged_into_id is None:
            return cur
        cur = c.merged_into_id
    return cur


# --------------------------------------------------------------------------- matching / expansion
_LEVEL_WEIGHT = {"specialism": 1.0, "subcategory": 0.85, "category": 0.6}


def _term_re(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"s?(?![a-z0-9])")


def score_text(text_: str, cats: list[Category]) -> list[tuple[Category, float]]:
    """Pure scoring: keyword/synonym hits (longer phrases worth more) x level weight; any exclusion hit => dropped."""
    t = re.sub(r"\s+", " ", text_.lower())
    out: list[tuple[Category, float]] = []
    for c in cats:
        if not c.enabled or c.merged_into_id is not None:
            continue
        if any(_term_re(x).search(t) for x in (c.excluded_keywords or [])):
            continue
        score = 0.0
        hits = 0
        for term in (c.keywords or []):
            if _term_re(term).search(t):
                score += 1.0 + 0.5 * (len(term.split()) - 1)
                hits += 1
        for term in (c.synonyms or []):
            if _term_re(term).search(t):
                score += 0.7 + 0.35 * (len(term.split()) - 1)
                hits += 1
        if hits:
            out.append((c, round(score * _LEVEL_WEIGHT.get(c.level, 0.6) / (1 + 0.02 * len(c.keywords or [])), 4)))
    out.sort(key=lambda x: (-x[1], x[0].slug))
    return out


async def match_category(session: AsyncSession, text_: str, *, limit: int = 5, min_score: float = 0.3) -> list[tuple[Category, float]]:
    cats = list((await session.execute(select(Category).where(Category.enabled.is_(True), Category.merged_into_id.is_(None)))).scalars())
    return [(c, s) for c, s in score_text(text_, cats) if s >= min_score][:limit]


async def expand_category_terms(
    session: AsyncSession, category_ids: list[int], *, include_descendants: bool = True
) -> DiscoveryQueryTerms:
    """Union of keywords/synonyms/SIC/OSM/sources of the categories (+ descendants).  Exclusions apply globally
    to the query.  Disabled and merged categories are skipped (merged ids are first resolved to their target)."""
    resolved = [await resolve_merged(session, i) for i in category_ids]
    ids = await descendant_ids(session, list(dict.fromkeys(resolved))) if include_descendants else list(dict.fromkeys(resolved))
    rows = (await session.execute(select(Category).where(Category.id.in_(ids)).order_by(Category.id))).scalars().all()
    t = DiscoveryQueryTerms()
    for c in rows:
        if not c.enabled or c.merged_into_id is not None:
            continue
        t.category_ids.append(c.id)
        t.keywords = _union(t.keywords, c.keywords or [])
        t.synonyms = _union(t.synonyms, c.synonyms or [])
        t.excluded_keywords = _union(t.excluded_keywords, c.excluded_keywords or [])
        t.sic_codes = _union(t.sic_codes, c.sic_codes or [])
        t.osm_tags = _union(t.osm_tags, c.osm_tags or [])
        t.default_sources = _union(t.default_sources, c.default_sources or [])
    return t
