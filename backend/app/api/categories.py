"""Business taxonomy: tree with counts, CRUD, merge, enable/disable, synonyms, import/export, directory."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Category, User
from ..security import ApiError, app_settings, audit, require_role
from .common import Ok, Page
from .contacts import ContactRow, contact_rows
from .filters import ContactFilterSpec, build_conditions, contact_filters
from .uploads import read_limited, sniff

router = APIRouter(prefix="/api/categories", tags=["categories"], dependencies=[Depends(require_role("viewer"))])
LEVELS = ["category", "subcategory", "specialism"]
LIST_FIELDS = ("keywords", "synonyms", "excluded_keywords", "sic_codes", "osm_tags", "default_sources")
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 20000


class CategoryOut(BaseModel):
    id: int
    parent_id: int | None
    slug: str
    name: str
    level: str
    keywords: list[str]
    synonyms: list[str]
    excluded_keywords: list[str]
    sic_codes: list[str]
    osm_tags: list[str]
    default_sources: list[str]
    enabled: bool
    merged_into_id: int | None = None
    updated_at: datetime | None = None


class CategoryNode(CategoryOut):
    business_count: int = 0
    email_count: int = 0
    published_count: int = 0
    eligible_count: int = 0
    countries: list[str] = Field(default_factory=list)
    last_run_at: datetime | None = None
    children: list[CategoryNode] = Field(default_factory=list)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None
    slug: str | None = Field(None, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    level: str | None = Field(None, pattern="^(category|subcategory|specialism)$")
    keywords: list[str] = Field(default_factory=list, max_length=500)
    synonyms: list[str] = Field(default_factory=list, max_length=500)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=500)
    sic_codes: list[str] = Field(default_factory=list, max_length=200)
    osm_tags: list[str] = Field(default_factory=list, max_length=200)
    default_sources: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("keywords", "synonyms", "excluded_keywords", "sic_codes", "osm_tags", "default_sources")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(s.strip() for s in v if s and s.strip()))[:500]


class CategoryUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    parent_id: int | None = None
    set_parent: bool = Field(False, description="true to apply parent_id (including null = make top level)")
    level: str | None = Field(None, pattern="^(category|subcategory|specialism)$")
    keywords: list[str] | None = None
    excluded_keywords: list[str] | None = None
    sic_codes: list[str] | None = None
    osm_tags: list[str] | None = None
    default_sources: list[str] | None = None


class SynonymsIn(BaseModel):
    synonyms: list[str] = Field(max_length=500)

    @field_validator("synonyms")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(s.strip() for s in v if s and s.strip()))


class CategoryMergeIn(BaseModel):
    target_id: int


class CategoryMergeOut(BaseModel):
    source_id: int
    target_id: int
    businesses_moved: int
    children_moved: int
    runs_updated: int


class ImportOut(BaseModel):
    created: int
    updated: int
    dry_run: bool
    errors: list[str] = Field(default_factory=list)


def _out(c: Category) -> CategoryOut:
    return CategoryOut(id=c.id, parent_id=c.parent_id, slug=c.slug, name=c.name, level=c.level, keywords=c.keywords or [], synonyms=c.synonyms or [],
                       excluded_keywords=c.excluded_keywords or [], sic_codes=c.sic_codes or [], osm_tags=c.osm_tags or [], default_sources=c.default_sources or [],
                       enabled=c.enabled, merged_into_id=c.merged_into_id, updated_at=c.updated_at)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100] or "category"


async def _unique_slug(session: AsyncSession, base: str) -> str:
    slug, n = base, 1
    while (await session.execute(select(Category.id).where(Category.slug == slug))).first():
        n += 1
        slug = f"{base}-{n}"[:120]
    return slug


async def _descendants(session: AsyncSession, cid: int) -> set[int]:
    rows = (await session.execute(text("WITH RECURSIVE t AS (SELECT id FROM categories WHERE id=:i UNION SELECT c.id FROM categories c JOIN t ON c.parent_id=t.id) SELECT id FROM t"), {"i": cid})).scalars()
    return set(rows)


async def _level_for(session: AsyncSession, parent_id: int | None, requested: str | None) -> str:
    if parent_id is None:
        return requested or "category"
    parent = await session.get(Category, parent_id)
    if parent is None:
        raise ApiError(422, "invalid_parent", f"Parent category {parent_id} does not exist.")
    idx = LEVELS.index(parent.level) + 1 if parent.level in LEVELS else 1
    if idx >= len(LEVELS):
        raise ApiError(422, "too_deep", "Categories can be nested at most three levels deep (category > subcategory > specialism).")
    return LEVELS[idx]


async def _counts(session: AsyncSession) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for cid, n, countries in (await session.execute(text("SELECT category_id, count(*), array_agg(DISTINCT country_code) FROM organisations WHERE category_id IS NOT NULL AND merged_into_id IS NULL GROUP BY 1"))).all():
        out[cid] = {"b": n, "e": 0, "p": 0, "el": 0, "countries": set(countries or [])}
    for cid, e, p, el in (await session.execute(text(
        """SELECT o.category_id, count(*), count(*) FILTER (WHERE e.origin IN ('published','corroborated')),
                  count(*) FILTER (WHERE e.marketing_status='eligible' AND e.suppressed_at IS NULL)
           FROM email_addresses e JOIN organisations o ON o.id=e.organisation_id WHERE o.category_id IS NOT NULL GROUP BY 1"""))).all():
        d = out.setdefault(cid, {"b": 0, "e": 0, "p": 0, "el": 0, "countries": set()})
        d["e"], d["p"], d["el"] = e, p, el
    for cid, ts in (await session.execute(text("SELECT category_id, max(coalesce(started_at, created_at)) FROM sourcing_runs WHERE category_id IS NOT NULL GROUP BY 1"))).all():
        out.setdefault(cid, {"b": 0, "e": 0, "p": 0, "el": 0, "countries": set()})["last_run"] = ts
    return out


def _build_tree(cats: list[Category], counts: dict[int, dict[str, Any]]) -> list[CategoryNode]:
    nodes = {c.id: CategoryNode(**_out(c).model_dump()) for c in cats}
    for cid, n in nodes.items():
        d = counts.get(cid, {})
        n.business_count, n.email_count, n.published_count, n.eligible_count = d.get("b", 0), d.get("e", 0), d.get("p", 0), d.get("el", 0)
        n.countries = sorted(d.get("countries", set()))
        n.last_run_at = d.get("last_run")
    roots: list[CategoryNode] = []
    for c in cats:
        node = nodes[c.id]
        if c.parent_id in nodes:
            nodes[c.parent_id].children.append(node)
        else:
            roots.append(node)

    def roll(n: CategoryNode) -> None:
        cs = set(n.countries)
        for ch in n.children:
            roll(ch)
            n.business_count += ch.business_count
            n.email_count += ch.email_count
            n.published_count += ch.published_count
            n.eligible_count += ch.eligible_count
            cs |= set(ch.countries)
            if ch.last_run_at and (n.last_run_at is None or ch.last_run_at > n.last_run_at):
                n.last_run_at = ch.last_run_at
        n.countries = sorted(cs)

    for r in roots:
        roll(r)
    key = lambda n: n.name.lower()  # noqa: E731

    def sort_rec(ns: list[CategoryNode]) -> None:
        ns.sort(key=key)
        for n in ns:
            sort_rec(n.children)

    sort_rec(roots)
    return roots


# --------------------------------------------------------------------------- list / tree
@router.get("", response_model=list[CategoryNode])
async def list_categories(include_disabled: bool = True, session: AsyncSession = Depends(get_session)) -> list[CategoryNode]:
    q = select(Category).order_by(Category.id)
    if not include_disabled:
        q = q.where(Category.enabled.is_(True))
    cats = list((await session.execute(q)).scalars())
    return _build_tree(cats, await _counts(session))


async def build_taxonomy_rows(session: AsyncSession) -> list[dict[str, Any]]:
    cats = list((await session.execute(select(Category).order_by(Category.id))).scalars())
    slug_of = {c.id: c.slug for c in cats}
    return [{"slug": c.slug, "name": c.name, "parent_slug": slug_of.get(c.parent_id), "level": c.level, "enabled": c.enabled,
             **{f: list(getattr(c, f) or []) for f in LIST_FIELDS}} for c in cats]


def taxonomy_to_csv(rows: list[dict[str, Any]]) -> str:
    from ..exports import csv_safe

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["slug", "name", "parent_slug", "level", "enabled", *LIST_FIELDS])
    for r in rows:
        w.writerow([csv_safe(r["slug"]), csv_safe(r["name"]), r["parent_slug"] or "", r["level"], str(r["enabled"]).lower(), *("|".join(csv_safe(x) for x in r[f]) for f in LIST_FIELDS)])
    return buf.getvalue()


@router.get("/export", response_class=Response, responses={200: {"content": {"application/json": {}, "text/csv": {}}}})
async def export_taxonomy(format: str = Query("json", pattern="^(json|csv)$"), session: AsyncSession = Depends(get_session)):
    rows = await build_taxonomy_rows(session)
    if format == "json":
        return Response(json.dumps({"categories": rows}, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="taxonomy.json"'})
    return Response(taxonomy_to_csv(rows), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="taxonomy.csv"'})


@router.post("/import", response_model=ImportOut)
async def import_taxonomy(request: Request, file: UploadFile = File(...), dry_run: bool = Query(False), user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> ImportOut:
    limit = min(MAX_IMPORT_BYTES, app_settings(request).max_upload_mb * 1024 * 1024)
    data = await read_limited(file, limit)
    if sniff(data) != "text":
        raise ApiError(422, "unsupported_file", "Taxonomy import accepts JSON or CSV files only.")
    text_data = data.decode("utf-8-sig", errors="replace")
    rows: list[dict[str, Any]]
    try:
        if text_data.lstrip().startswith(("{", "[")):
            parsed = json.loads(text_data)
            rows = parsed.get("categories") if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list):
                raise ValueError("expected a list of categories under 'categories'")
        else:
            rows = []
            for r in csv.DictReader(io.StringIO(text_data)):
                d: dict[str, Any] = {k.strip(): (v or "").strip() for k, v in r.items() if k}
                for f in LIST_FIELDS:
                    d[f] = [x.strip() for x in re.split(r"[|;]", d.get(f, "")) if x.strip()]
                d["enabled"] = d.get("enabled", "true").lower() not in ("false", "0", "no")
                rows.append(d)
    except (ValueError, json.JSONDecodeError) as e:
        raise ApiError(422, "invalid_file", f"Could not parse the file: {str(e)[:200]}") from None
    if len(rows) > MAX_IMPORT_ROWS:
        raise ApiError(413, "too_many_rows", f"At most {MAX_IMPORT_ROWS} categories can be imported at once.")
    errors: list[str] = []
    clean: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"].strip():
            errors.append(f"row {i}: 'name' is required")
            continue
        slug = (r.get("slug") or slugify(r["name"])).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug):
            errors.append(f"row {i}: invalid slug '{str(slug)[:40]}'")
            continue
        if r.get("level") and r["level"] not in LEVELS:
            errors.append(f"row {i}: level must be one of {', '.join(LEVELS)}")
            continue
        item: dict[str, Any] = {"slug": slug, "name": r["name"].strip()[:200], "parent_slug": (r.get("parent_slug") or None), "level": r.get("level"), "enabled": bool(r.get("enabled", True))}
        for f in LIST_FIELDS:
            v = r.get(f, [])
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                errors.append(f"row {i}: '{f}' must be a list of strings")
                break
            item[f] = [x.strip() for x in v if x.strip()][:500]
        else:
            clean[slug] = item
    for slug, item in clean.items():
        if item["parent_slug"] and item["parent_slug"] not in clean and (await session.execute(select(Category.id).where(Category.slug == item["parent_slug"]))).first() is None:
            errors.append(f"'{slug}': parent '{item['parent_slug']}' not found")
    if errors:
        raise ApiError(422, "invalid_taxonomy", f"The file has {len(errors)} problem(s); nothing was imported. First: {errors[0]}", errors[:50])
    existing = {c.slug: c for c in (await session.execute(select(Category))).scalars()}
    created = updated = 0
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(slug: str, stack: tuple[str, ...] = ()) -> None:
        if slug in seen or slug not in clean:
            return
        if slug in stack:
            raise ApiError(422, "invalid_taxonomy", f"Category '{slug}' is its own ancestor.")
        p = clean[slug]["parent_slug"]
        if p:
            visit(p, (*stack, slug))
        seen.add(slug)
        ordered.append(clean[slug])

    for slug in clean:
        visit(slug)
    for item in ordered:
        parent = existing.get(item["parent_slug"]) if item["parent_slug"] else None
        c = existing.get(item["slug"])
        level = item["level"] or (LEVELS[min(LEVELS.index(parent.level) + 1, 2)] if parent else "category")
        if c is None:
            c = Category(slug=item["slug"], name=item["name"], parent_id=parent.id if parent else None, level=level, enabled=item["enabled"], **{f: item[f] for f in LIST_FIELDS})
            session.add(c)
            await session.flush()
            existing[c.slug] = c
            created += 1
        else:
            c.name, c.level, c.enabled = item["name"], level, item["enabled"]
            c.parent_id = parent.id if parent else None
            for f in LIST_FIELDS:
                setattr(c, f, item[f])
            updated += 1
    if dry_run:
        await session.rollback()
    else:
        await audit(session, user, "taxonomy.import", "category", None, {"created": created, "updated": updated})
        await session.commit()
    return ImportOut(created=created, updated=updated, dry_run=dry_run)


# --------------------------------------------------------------------------- CRUD
@router.post("", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> CategoryOut:
    level = await _level_for(session, body.parent_id, body.level)
    slug = body.slug or await _unique_slug(session, slugify(body.name))
    c = Category(slug=slug, name=body.name.strip(), parent_id=body.parent_id, level=level, keywords=body.keywords, synonyms=body.synonyms,
                 excluded_keywords=body.excluded_keywords, sic_codes=body.sic_codes, osm_tags=body.osm_tags, default_sources=body.default_sources)
    session.add(c)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "slug_in_use", f"A category with slug '{slug}' already exists.") from None
    await audit(session, user, "category.create", "category", c.id, {"name": c.name, "parent_id": c.parent_id})
    await session.commit()
    await session.refresh(c)
    return _out(c)


@router.get("/{category_id}", response_model=CategoryNode)
async def get_category(category_id: int, session: AsyncSession = Depends(get_session)) -> CategoryNode:
    cats = list((await session.execute(select(Category))).scalars())
    tree = _build_tree(cats, await _counts(session))

    def find(ns: list[CategoryNode]) -> CategoryNode | None:
        for n in ns:
            if n.id == category_id:
                return n
            r = find(n.children)
            if r:
                return r
        return None

    node = find(tree)
    if node is None:
        raise ApiError(404, "not_found", "Category not found.")
    return node


async def _get(session: AsyncSession, category_id: int) -> Category:
    c = await session.get(Category, category_id)
    if c is None:
        raise ApiError(404, "not_found", "Category not found.")
    return c


@router.put("/{category_id}", response_model=CategoryOut)
async def update_category(category_id: int, body: CategoryUpdateIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> CategoryOut:
    c = await _get(session, category_id)
    if body.set_parent:
        if body.parent_id is not None and body.parent_id in await _descendants(session, c.id):
            raise ApiError(422, "cycle", "A category cannot be moved under itself or one of its own sub-categories.")
        c.level = await _level_for(session, body.parent_id, body.level)
        c.parent_id = body.parent_id
    elif body.level:
        c.level = body.level
    if body.name:
        c.name = body.name.strip()
    for f in ("keywords", "excluded_keywords", "sic_codes", "osm_tags", "default_sources"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, list(dict.fromkeys(s.strip() for s in v if s.strip()))[:500])
    await audit(session, user, "category.update", "category", c.id, {"fields": sorted(body.model_dump(exclude_unset=True))})
    await session.commit()
    await session.refresh(c)
    return _out(c)


@router.put("/{category_id}/synonyms", response_model=CategoryOut)
async def set_category_synonyms(category_id: int, body: SynonymsIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> CategoryOut:
    c = await _get(session, category_id)
    c.synonyms = body.synonyms
    await audit(session, user, "category.synonyms", "category", c.id, {"count": len(body.synonyms)})
    await session.commit()
    await session.refresh(c)
    return _out(c)


async def _set_enabled(session: AsyncSession, user: User, category_id: int, enabled: bool) -> CategoryOut:
    c = await _get(session, category_id)
    ids = await _descendants(session, c.id) if not enabled else {c.id}
    await session.execute(text("UPDATE categories SET enabled=:e WHERE id = ANY(:ids)"), {"e": enabled, "ids": list(ids)})
    await audit(session, user, "category.enable" if enabled else "category.disable", "category", c.id, {"affected": len(ids)})
    await session.commit()
    await session.refresh(c)
    await session.refresh(c)
    return _out(c)


@router.post("/{category_id}/disable", response_model=CategoryOut)
async def disable_category(category_id: int, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> CategoryOut:
    return await _set_enabled(session, user, category_id, False)


@router.post("/{category_id}/enable", response_model=CategoryOut)
async def enable_category(category_id: int, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> CategoryOut:
    return await _set_enabled(session, user, category_id, True)


@router.post("/{category_id}/merge", response_model=CategoryMergeOut)
async def merge_category(category_id: int, body: CategoryMergeIn, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> CategoryMergeOut:
    src = await _get(session, category_id)
    tgt = await _get(session, body.target_id)
    if src.id == tgt.id:
        raise ApiError(422, "invalid_merge", "A category cannot be merged into itself.")
    if tgt.id in await _descendants(session, src.id):
        raise ApiError(422, "invalid_merge", "Cannot merge a category into one of its own sub-categories.")
    moved = (await session.execute(text("UPDATE organisations SET category_id=:t WHERE category_id=:s"), {"t": tgt.id, "s": src.id})).rowcount or 0
    kids = (await session.execute(text("UPDATE categories SET parent_id=:t WHERE parent_id=:s"), {"t": tgt.id, "s": src.id})).rowcount or 0
    runs = (await session.execute(text("UPDATE sourcing_runs SET category_id = CASE WHEN category_id=:s THEN :t ELSE category_id END, destination_category_id = CASE WHEN destination_category_id=:s THEN :t ELSE destination_category_id END WHERE category_id=:s OR destination_category_id=:s"), {"t": tgt.id, "s": src.id})).rowcount or 0
    tgt.synonyms = list(dict.fromkeys([*(tgt.synonyms or []), src.name, *(src.synonyms or [])]))
    tgt.keywords = list(dict.fromkeys([*(tgt.keywords or []), *(src.keywords or [])]))
    src.enabled, src.merged_into_id = False, tgt.id
    await audit(session, user, "category.merge", "category", src.id, {"into": tgt.id, "businesses": moved})
    await session.commit()
    return CategoryMergeOut(source_id=src.id, target_id=tgt.id, businesses_moved=moved, children_moved=kids, runs_updated=runs)


@router.delete("/{category_id}", response_model=Ok)
async def delete_category(category_id: int, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> Ok:
    c = await _get(session, category_id)
    used = (await session.execute(text("SELECT (SELECT count(*) FROM organisations WHERE category_id=:i) + (SELECT count(*) FROM categories WHERE parent_id=:i)"), {"i": c.id})).scalar_one()
    if used:
        raise ApiError(409, "category_in_use", "This category still has businesses or sub-categories. Merge it into another category or disable it instead.")
    await audit(session, user, "category.delete", "category", c.id, {"name": c.name})
    await session.delete(c)
    await session.commit()
    return Ok(message="Category deleted.")


@router.get("/{cat_id}/contacts", response_model=Page[ContactRow])
async def category_contacts(
    cat_id: int, f: ContactFilterSpec = Depends(contact_filters), sort: str = "id", direction: str = "desc", limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[ContactRow]:
    await _get(session, cat_id)
    f = f.model_copy(update={"category_id": [cat_id]})
    return await contact_rows(session, await build_conditions(session, f), sort, direction, limit, cursor)


CategoryNode.model_rebuild()
