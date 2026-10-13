"""Shared API helpers: pagination (keyset), LIKE escaping, approximate counts, response envelopes."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, DateTime, Float, String, literal, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement, Select

from ..security import ApiError

MAX_LIMIT = 500


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    total_estimate: int | None = Field(None, description="Row count (exact when small, capped/approximate otherwise); null when unknown")
    approximate: bool = False


class Ok(BaseModel):
    ok: bool = True
    message: str | None = None


class CountResult(BaseModel):
    affected: int
    detail: dict[str, Any] = Field(default_factory=dict)


def escape_like(s: str) -> str:
    """Escape LIKE wildcards; use with ``escape='\\\\'``."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def prefix_pattern(q: str) -> str:
    return escape_like(q.strip().lower()) + "%"


def clean_q(q: str | None) -> str | None:
    if q is None:
        return None
    q = q.strip()
    return q[:100] or None


# --------------------------------------------------------------------------- keyset pagination
@dataclass(frozen=True)
class SortSpec:
    column: ColumnElement
    kind: str  # int | str | dt | float


_KIND_TYPES = {"int": BigInteger(), "str": String(), "dt": DateTime(timezone=True), "float": Float()}


def _enc(sort: str, direction: str, value: Any, row_id: int) -> str:
    if isinstance(value, datetime):
        value = value.isoformat()
    raw = json.dumps({"s": sort, "d": direction, "v": value, "i": row_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _dec(cursor: str) -> dict[str, Any]:
    try:
        pad = "=" * (-len(cursor) % 4)
        d = json.loads(base64.urlsafe_b64decode(cursor + pad))
        if not isinstance(d, dict) or not {"s", "d", "v", "i"} <= d.keys():
            raise ValueError
        return d
    except Exception:
        raise ApiError(422, "invalid_cursor", "The pagination cursor is invalid. Start again from the first page.") from None


def _coerce(kind: str, v: Any) -> Any:
    try:
        if kind == "int":
            return int(v)
        if kind == "float":
            return float(v)
        if kind == "dt":
            dt = datetime.fromisoformat(str(v))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return str(v)
    except Exception:
        raise ApiError(422, "invalid_cursor", "The pagination cursor is invalid. Start again from the first page.") from None


def check_sort(sorts: dict[str, SortSpec], sort: str) -> SortSpec:
    if sort not in sorts:
        raise ApiError(422, "invalid_sort", f"Cannot sort by '{sort}'. Allowed: {', '.join(sorted(sorts))}.")
    return sorts[sort]


def check_direction(direction: str) -> str:
    d = direction.lower()
    if d not in ("asc", "desc"):
        raise ApiError(422, "invalid_direction", "direction must be 'asc' or 'desc'.")
    return d


def apply_keyset(
    stmt: Select, sorts: dict[str, SortSpec], sort: str, direction: str, id_col: ColumnElement, cursor: str | None, limit: int
) -> Select:
    """Add keyset WHERE, ORDER BY and LIMIT (limit+1 to detect another page). Select must already include
    ``sort_expr.label('_sv')`` and ``id_col.label('_id')``."""
    spec = check_sort(sorts, sort)
    direction = check_direction(direction)
    if cursor:
        c = _dec(cursor)
        if c["s"] != sort or c["d"] != direction:
            raise ApiError(422, "invalid_cursor", "The cursor does not belong to this sort order. Start again from the first page.")
        val = _coerce(spec.kind, c["v"])
        lhs = tuple_(spec.column, id_col)
        rhs = tuple_(literal(val, _KIND_TYPES[spec.kind]), literal(int(c["i"]), BigInteger()))
        stmt = stmt.where(lhs < rhs if direction == "desc" else lhs > rhs)
    order = (spec.column.desc(), id_col.desc()) if direction == "desc" else (spec.column.asc(), id_col.asc())
    return stmt.order_by(*order).limit(limit + 1)


def finish_page(rows: list[Any], sort: str, direction: str, limit: int) -> tuple[list[Any], str | None]:
    """Trim the extra row and build next_cursor from the last returned row's ``_sv`` / ``_id``."""
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    m = last._mapping if hasattr(last, "_mapping") else last
    return rows, _enc(sort, direction, m["_sv"], m["_id"])


async def table_estimate(session: AsyncSession, table: str) -> int:
    """Planner row estimate (pg_class.reltuples); -1/0 when never analysed."""
    v = (await session.execute(text("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(:t)"), {"t": table})).scalar()
    return int(v or 0)


async def table_count(session: AsyncSession, table: str, threshold: int = 500_000, where: str = "") -> tuple[int, bool]:
    """(count, approximate). Exact for small tables, planner estimate for big ones. ``table``/``where`` are code constants."""
    est = await table_estimate(session, table)
    if est > threshold and not where:
        return est, True
    n = (await session.execute(text(f"SELECT count(*) FROM {table} {where}"))).scalar_one()
    return int(n), False


async def capped_count(session: AsyncSession, count_stmt: Select, cap: int = 10_000, timeout_ms: int = 800) -> tuple[int | None, bool]:
    """Count rows of ``count_stmt`` (a select of one literal column with filters) up to ``cap``.

    Returns (count, capped). Uses a savepoint + statement_timeout so a slow count never breaks the request."""
    try:
        async with session.begin_nested():
            await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            from sqlalchemy import func, select

            n = (await session.execute(select(func.count()).select_from(count_stmt.limit(cap + 1).subquery()))).scalar_one()
        return min(int(n), cap), n > cap
    except Exception:
        return None, False


def csv_ints(v: str | None, name: str, max_items: int = 100) -> list[int]:
    if not v:
        return []
    out: list[int] = []
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise ApiError(422, "invalid_filter", f"'{name}' must be a comma separated list of integers.") from None
    if len(out) > max_items:
        raise ApiError(422, "invalid_filter", f"'{name}' accepts at most {max_items} values.")
    return out


def csv_strs(v: str | None, name: str, max_items: int = 100, max_len: int = 100) -> list[str]:
    if not v:
        return []
    out = [p.strip() for p in v.split(",") if p.strip()]
    if len(out) > max_items or any(len(p) > max_len for p in out):
        raise ApiError(422, "invalid_filter", f"'{name}' has too many or too long values.")
    return out


def check_enum(v: str | None, allowed: set[str] | tuple[str, ...], name: str) -> str | None:
    if v is None or v == "":
        return None
    if v not in allowed:
        raise ApiError(422, "invalid_filter", f"'{name}' must be one of: {', '.join(sorted(allowed))}.")
    return v


def check_enums(v: str | None, allowed: set[str] | tuple[str, ...], name: str) -> list[str]:
    out = csv_strs(v, name)
    bad = [x for x in out if x not in allowed]
    if bad:
        raise ApiError(422, "invalid_filter", f"'{name}' has invalid value(s) {bad}; allowed: {', '.join(sorted(allowed))}.")
    return out


def country_code(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip().upper()
    if len(v) != 2 or not v.isalpha():
        raise ApiError(422, "invalid_filter", "Country must be a 2-letter ISO code.")
    return v


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
