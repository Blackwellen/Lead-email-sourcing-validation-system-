"""Source adapters: catalogue merged with DB state, enable/priority/config, health, custom file upload."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import SourceAdapterState, User
from ..security import ApiError, app_settings, audit, require_role
from ..settings_store import get_setting, set_setting
from ..sources import secret_config as sc
from ..sources.access_policy import config_problems, is_path_key, is_url_key
from .registry import adapter_catalog, optional
from .runs_support import uploads_dir
from .uploads import read_limited, sniff

log = logging.getLogger("api.sources")
router = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_role("viewer"))])
SECRET_KEY_RE = sc.SECRET_KEY_RE  # one definition (app.sources.secret_config)
_totals_cache: dict[str, Any] = {"t": 0.0, "v": None}


class SourceHealthOut(BaseModel):
    ok: bool | None = None
    status: str = "unknown"
    message: str = ""
    checked_at: datetime | None = None


class SourceOut(BaseModel):
    adapter_id: str
    name: str
    registered: bool
    source_type: str | None = None
    country_coverage: list[str] = Field(default_factory=list)
    licence: str | None = None
    source_url: str | None = None
    access_requirements: str | None = None
    automated_collection_permitted: bool | None = None
    requires_config: bool = False
    info_only: bool = False
    attribution: str | None = None
    notes: str | None = None
    live_verified: bool = False
    enabled: bool
    priority: int
    configured: bool
    config: dict[str, Any] = Field(default_factory=dict, description="Secret-looking values are masked")
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    runs_ok: int = 0
    runs_failed: int = 0
    businesses_recovered: int = 0
    last_error: str | None = None
    total_businesses: int = 0
    total_published_emails: int = 0
    health: SourceHealthOut = Field(default_factory=SourceHealthOut)
    rate_policy: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    fields_available: list[str] = Field(default_factory=list)
    cost: str | None = None
    credentials: str | None = None
    refresh_frequency: str | None = None
    allowed_usage: str | None = None
    access_method: str | None = None
    config_schema: list[ConfigKeyOut] = Field(default_factory=list, description="permitted configuration keys (PUT /config rejects anything else)")


class ConfigKeyOut(BaseModel):
    key: str
    type: Literal["string", "integer", "boolean", "path", "url", "list", "object"]
    secret: bool = False
    description: str = ""


class PriorityIn(BaseModel):
    priority: int = Field(ge=0, le=10000)


class ConfigIn(BaseModel):
    config: dict[str, Any]


class UploadOut(BaseModel):
    upload_id: str
    kind: str
    size: int
    path: str
    suggested_source_params: dict[str, Any]
    report: dict[str, Any] | None = None


_VOCAB: dict[str, str] = {
    "path": "path", "file_path": "path", "rr_path": "path", "current_path": "path", "previous_path": "path", "previous_index": "path", "paths": "list",
    "url": "url", "base_url": "url", "auto_download": "boolean", "authorised": "boolean", "country_code": "string", "sheet": "string",
    "format": "string", "delta": "string", "mapping": "object", "days_back": "integer", "published_from": "string", "published_to": "string",
    "notice_types": "list", "page_size": "integer", "start_date": "string", "end_date": "string", "award_type_codes": "list", "naics_codes": "list",
    "recipient_search_text": "string", "max_pages": "integer", "min_awards": "integer", "min_award_years": "integer", "max_queries": "integer",
    "max_keywords": "integer", "results_per_query": "integer", "authority_codes": "list", "api_key": "string", "limit": "integer",
    "permission_basis": "string", "permission_note": "string",
}


def config_schema_for(info: dict[str, Any] | None) -> list[ConfigKeyOut]:
    """Permitted config keys of an adapter: the keys its ``access_requirements`` text names (quoted or from the shared vocabulary),
    credentials when it needs them, and the permission fields every source accepts."""
    keys: dict[str, str] = {"permission_basis": "string", "permission_note": "string", "mapping": "object"}
    text_ = (info or {}).get("access_requirements") or ""
    for m in re.finditer(r"[A-Za-z_]{3,40}", text_):
        w = m.group(0)
        if w in _VOCAB:
            keys.setdefault(w, _VOCAB[w])
    for q in re.findall(r"'([a-z_]{2,40})'", text_):
        keys.setdefault(q, _VOCAB.get(q, "string"))
    if (info or {}).get("credentials") in ("required", "optional") or "api key" in text_.lower() or "api_key" in text_:
        keys.setdefault("api_key", "string")
    out = []
    for k, t in sorted(keys.items()):
        if t == "string" and is_path_key(k):
            t = "path"
        elif t == "string" and is_url_key(k):
            t = "url"
        out.append(ConfigKeyOut(key=k, type=t, secret=sc.is_secret_key(k)))  # type: ignore[arg-type]
    return out


def mask_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return sc.mask_config(cfg)


async def _totals(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    if _totals_cache["v"] and time.monotonic() - _totals_cache["t"] < 20:
        return _totals_cache["v"]
    biz = {r[0]: r[1] for r in (await session.execute(text("SELECT discovery_source, count(*) FROM organisations WHERE discovery_source IS NOT NULL AND merged_into_id IS NULL GROUP BY 1"))).all()}
    em = {r[0]: r[1] for r in (await session.execute(text(
        "SELECT o.discovery_source, count(*) FROM email_addresses e JOIN organisations o ON o.id=e.organisation_id WHERE e.origin IN ('published','corroborated') AND o.discovery_source IS NOT NULL GROUP BY 1"))).all()}
    _totals_cache.update(t=time.monotonic(), v=(biz, em))
    return biz, em


async def _health_of(session: AsyncSession, aid: str) -> SourceHealthOut:
    h = await get_setting(session, f"source_health.{aid}", None)
    if not h:
        return SourceHealthOut()
    return SourceHealthOut(ok=h.get("ok"), status=h.get("status", "unknown"), message=h.get("message", ""), checked_at=datetime.fromisoformat(h["checked_at"]) if h.get("checked_at") else None)


async def build_sources(session: AsyncSession) -> list[SourceOut]:
    cat = adapter_catalog()
    states = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    biz, em = await _totals(session)
    ids = list(cat or {})
    for sid in list(states) + list(biz):
        if sid not in ids:
            ids.append(sid)
    out: list[SourceOut] = []
    for aid in ids:
        info = (cat or {}).get(aid)
        st = states.get(aid)
        cfg = (st.config if st else {}) or {}
        requires = bool(info and info.get("requires_config"))
        out.append(SourceOut(
            adapter_id=aid, name=(info or {}).get("name", aid), registered=info is not None, source_type=(info or {}).get("source_type"),
            country_coverage=list((info or {}).get("country_coverage") or []), licence=(info or {}).get("licence"), source_url=(info or {}).get("source_url"),
            access_requirements=(info or {}).get("access_requirements"), automated_collection_permitted=(info or {}).get("automated_collection_permitted"),
            requires_config=requires, info_only=bool(info and info.get("info_only")), attribution=(info or {}).get("attribution"), notes=(info or {}).get("notes"),
            live_verified=bool(info and info.get("live_verified")), enabled=st.enabled if st else False,
            priority=st.priority if st else int((info or {}).get("default_priority", 100)), configured=(not requires) or bool(cfg),
            config=mask_config(cfg), last_run_at=st.last_run_at if st else None, last_success_at=st.last_success_at if st else None,
            runs_ok=st.runs_ok if st else 0, runs_failed=st.runs_failed if st else 0, businesses_recovered=st.businesses_recovered if st else 0,
            last_error=st.last_error if st else None, total_businesses=biz.get(aid, 0), total_published_emails=em.get(aid, 0), health=await _health_of(session, aid),
            rate_policy=(info or {}).get("rate_policy") or {}, retry_policy=(info or {}).get("retry_policy") or {},
            fields_available=list((info or {}).get("fields_available") or []), cost=(info or {}).get("cost"), credentials=(info or {}).get("credentials"),
            refresh_frequency=(info or {}).get("refresh_frequency"), allowed_usage=(info or {}).get("allowed_usage"), access_method=(info or {}).get("access_method"),
            config_schema=config_schema_for(info),
        ))
    out.sort(key=lambda s: (s.priority, s.adapter_id))
    return out


async def _state(session: AsyncSession, aid: str, create: bool = True) -> SourceAdapterState:
    cat = adapter_catalog()
    if cat is not None and aid not in cat:
        raise ApiError(404, "not_found", f"Unknown source '{aid}'.")
    st = await session.get(SourceAdapterState, aid)
    if st is None:
        if cat is None:
            raise ApiError(404, "not_found", f"Unknown source '{aid}'.")
        info = cat[aid]
        st = SourceAdapterState(adapter_id=aid, enabled=False, priority=int(info.get("default_priority", 100)), config={})
        session.add(st)
        await session.flush()
    return st


@router.get("", response_model=list[SourceOut])
async def list_sources(session: AsyncSession = Depends(get_session)) -> list[SourceOut]:
    return await build_sources(session)


@router.get("/{adapter_id}", response_model=SourceOut)
async def get_source(adapter_id: str, session: AsyncSession = Depends(get_session)) -> SourceOut:
    for s in await build_sources(session):
        if s.adapter_id == adapter_id:
            return s
    raise ApiError(404, "not_found", "Source not found.")


async def _one(session: AsyncSession, aid: str) -> SourceOut:
    _totals_cache["v"] = None
    return await get_source(aid, session)


@router.post("/{adapter_id}/enable", response_model=SourceOut)
async def enable_source(adapter_id: str, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> SourceOut:
    st = await _state(session, adapter_id)
    cat = adapter_catalog() or {}
    info = cat.get(adapter_id, {})
    if info.get("info_only") or info.get("automated_collection_permitted") is False:
        raise ApiError(409, "not_permitted", "This source does not permit automated collection, so it cannot be enabled.")
    st.enabled = True
    await audit(session, user, "source.enable", "source", adapter_id)
    await session.commit()
    return await _one(session, adapter_id)


@router.post("/{adapter_id}/disable", response_model=SourceOut)
async def disable_source(adapter_id: str, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> SourceOut:
    st = await _state(session, adapter_id)
    st.enabled = False
    await audit(session, user, "source.disable", "source", adapter_id)
    await session.commit()
    return await _one(session, adapter_id)


@router.put("/{adapter_id}/priority", response_model=SourceOut)
async def set_source_priority(adapter_id: str, body: PriorityIn, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> SourceOut:
    st = await _state(session, adapter_id)
    st.priority = body.priority
    await audit(session, user, "source.priority", "source", adapter_id, {"priority": body.priority})
    await session.commit()
    return await _one(session, adapter_id)


@router.put("/{adapter_id}/config", response_model=SourceOut)
async def set_source_config(adapter_id: str, body: ConfigIn, request: Request, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> SourceOut:
    import json

    if len(json.dumps(body.config)) > 32 * 1024:
        raise ApiError(413, "payload_too_large", "Source configuration is limited to 32 KB.")
    st = await _state(session, adapter_id)
    old = st.config or {}
    new: dict[str, Any] = {}
    allowed = {c.key: c for c in config_schema_for((adapter_catalog() or {}).get(adapter_id))}
    settings = app_settings(request)
    for k, v in body.config.items():
        spec = allowed.get(k)
        if spec is None and not sc.is_secret_key(k):
            raise ApiError(422, "invalid_config_key", f"'{k[:60]}' is not a permitted configuration key for this source (permitted: {', '.join(sorted(allowed))}).")
        if spec is not None and v not in (None, "********"):
            ok = {"integer": isinstance(v, int) and not isinstance(v, bool), "boolean": isinstance(v, bool), "list": isinstance(v, list), "object": isinstance(v, dict),
                  "path": isinstance(v, str), "url": isinstance(v, str) and v.lower().startswith(("http://", "https://")), "string": isinstance(v, str | int | float)}[spec.type]
            if not ok:
                raise ApiError(422, "invalid_config_value", f"'{k}' must be of type {spec.type}.")
        if v == sc.MASK and k in old:  # unchanged masked secret
            new[k] = old[k]
            continue
        problems = config_problems({k: v}, settings, allow_permission_keys=True)
        if problems:
            code = "invalid_path" if is_path_key(k) else "invalid_config_value"
            raise ApiError(422, code, f"'{k}': {problems[0][1]}")
        if (sc.is_secret_key(k) or (spec is not None and spec.secret)) and isinstance(v, str) and v:
            v = sc.seal_value(v)  # secrets are stored sealed (Fernet), never in plain text
        new[k] = v
    if new.get("permission_basis") and new.get("permission_basis") != old.get("permission_basis"):
        new["permission_basis_by"] = user.id
    elif "permission_basis" in new and "permission_basis_by" in old:
        new["permission_basis_by"] = old["permission_basis_by"]
    st.config = new
    await audit(session, user, "source.config", "source", adapter_id,
                {"keys": sorted(new), "permission_basis": new.get("permission_basis"), "before_permission_basis": old.get("permission_basis")})
    await session.commit()
    return await _one(session, adapter_id)


@router.post("/{adapter_id}/health", response_model=SourceHealthOut)
async def refresh_source_health(adapter_id: str, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> SourceHealthOut:
    base = optional("app.sources.base")
    if base is None:
        raise ApiError(501, "not_available", "The source registry is not available in this deployment.")
    st = await session.get(SourceAdapterState, adapter_id)
    try:
        adapter = base.get_adapter(adapter_id, sc.open_config(st.config) if st else None)
    except KeyError:
        raise ApiError(404, "not_found", f"Unknown source '{adapter_id}'.") from None
    try:
        h = await asyncio.wait_for(adapter.health(), timeout=15)
        res = {"ok": bool(h.ok), "status": h.status, "message": (h.message or "")[:500]}
    except TimeoutError:
        res = {"ok": False, "status": "unavailable", "message": "The health check timed out after 15 seconds."}
    except Exception as e:
        res = {"ok": False, "status": "unavailable", "message": f"{type(e).__name__}: {str(e)[:300]}"}
    res["checked_at"] = datetime.now(UTC).isoformat()
    await set_setting(session, f"source_health.{adapter_id}", res)
    await audit(session, user, "source.health_refresh", "source", adapter_id, {"status": res["status"]})
    await session.commit()
    return SourceHealthOut(ok=res["ok"], status=res["status"], message=res["message"], checked_at=datetime.fromisoformat(res["checked_at"]))


@router.post("/upload", response_model=UploadOut, status_code=201)
async def upload_source_file(request: Request, file: UploadFile = File(...), user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> UploadOut:
    """Store an operator-supplied CSV/XLSX under data_dir/uploads with a random name. The file is never executed or served back."""
    settings = app_settings(request)
    data = await read_limited(file, settings.max_upload_mb * 1024 * 1024)
    kind = sniff(data)
    if kind == "text":
        head = data[:4096].lstrip().lower()
        if head.startswith((b"{", b"[")):
            raise ApiError(422, "unsupported_file", "JSON is not accepted here; upload a CSV or XLSX file.")
        ext = "csv"
    else:
        ext = "xlsx"
    d = uploads_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    uid = secrets.token_hex(16)
    path = d / f"{uid}.{ext}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    report = None
    mod = optional("app.sources.csv_import")
    if mod is not None and hasattr(mod, "validate_import_file"):
        try:
            rep = await asyncio.to_thread(mod.validate_import_file, path)
            report = dataclasses.asdict(rep)
        except Exception as e:  # rejected as a whole: remove the file
            path.unlink(missing_ok=True)
            raise ApiError(422, "invalid_import_file", f"The file was rejected: {str(e)[:300]}") from None
    await audit(session, user, "source.upload", "upload", uid, {"kind": ext, "size": len(data)})
    await session.commit()
    return UploadOut(upload_id=uid, kind=ext, size=len(data), path=str(path), suggested_source_params={"csv_import": {"path": str(path)}}, report=report)


