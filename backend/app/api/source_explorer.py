"""Source Explorer: search the full source catalogue (adapters + directory catalogue), inspect, configure, enable and feed it.

Roles: viewer = read, operator = configure / enable / disable / import. Secrets (API keys ...) are sealed with the application's
Fernet helper at rest and are never returned (a mask is). Uploaded datasets are stored under ``data_dir/uploads`` with a random
name, size- and type-limited, and only their path is recorded in the adapter configuration.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import re
import secrets
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import SourceAdapterState, User
from ..security import ApiError, app_settings, audit, require_role
from ..settings_store import get_setting
from ..sources import secret_config as sc
from ..sources.base import get_adapter, list_adapters
from ..sources.catalogue_full import ACCESS_METHODS, search_catalogue, source_catalogue
from .common import Page, check_enum, clean_q
from .runs_support import uploads_dir
from .uploads import read_limited

log = logging.getLogger("api.source_explorer")
router = APIRouter(prefix="/api/source-explorer", tags=["source-explorer"], dependencies=[Depends(require_role("viewer"))])

PERMISSION_BASES = ("open_licence", "licence_agreement", "api_terms", "written_permission", "public_domain")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
PATH_KEYS = {"path", "file_path", "file", "rr_path", "columnar_index_path"}
MAX_KEYS = 30
MAX_CONFIG_BYTES = 16 * 1024
IMPORT_EXTS = {"zip": "zip", "gz": "gz", "bz2": "bz2", "xlsx": "xlsx", "text": "csv"}


# --------------------------------------------------------------------------- models
class HealthOut(BaseModel):
    status: str
    message: str = ""
    checked_at: datetime | None = None
    probed: bool = False


class ExplorerItem(BaseModel):
    id: str
    adapter_id: str | None
    catalogue_id: str | None
    name: str
    countries: list[str]
    source_type: str
    access_method: str
    licence: str
    cost: str
    credentials: str
    credentials_required: bool
    refresh_frequency: str
    automated_allowed: bool
    industries: list[str]
    configured: bool
    enabled: bool
    info_only: bool
    live_verified: bool
    url: str
    health: HealthOut
    score: float | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class PermissionOut(BaseModel):
    status: str = Field(description="not_automatable | restricted | permission_declared | open_licence | permission_needed")
    basis: str | None = None
    note: str | None = None
    can_enable: bool
    reason: str


class RequirementsOut(BaseModel):
    requires_config: bool
    credentials: str
    credentials_note: str
    access_requirements: str
    accepts_file_upload: bool
    config_keys_set: list[str]
    secret_keys_set: list[str] = Field(description="names only; values are never returned")
    permitted_permission_bases: list[str]


class StateOut(BaseModel):
    enabled: bool
    priority: int | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    runs_ok: int = 0
    runs_failed: int = 0
    businesses_recovered: int = 0
    last_error: str | None = None


class ExplorerDetail(ExplorerItem):
    notes: str
    usage: str
    automated_extraction: str | None = None
    requirements: RequirementsOut
    permission: PermissionOut
    state: StateOut
    rate_policy: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict, description="Secret values are masked")


class ConfigureIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict, description="Plain keys are stored as given; secret-looking keys are sealed")
    permission_basis: str | None = Field(None, description="how the operator is permitted to collect from this source")
    permission_note: str | None = Field(None, max_length=500)
    remove_keys: list[str] = Field(default_factory=list, max_length=MAX_KEYS)


class ImportOut(BaseModel):
    adapter_id: str
    upload_id: str
    kind: str
    size: int
    path: str
    config_key: str = "path"


class ToggleOut(BaseModel):
    adapter_id: str
    enabled: bool


# --------------------------------------------------------------------------- helpers
async def _states(session: AsyncSession) -> dict[str, SourceAdapterState]:
    return {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}


async def _catalogue(session: AsyncSession) -> tuple[list[dict[str, Any]], dict[str, SourceAdapterState]]:
    st = await _states(session)
    configs = {k: dict(v.config or {}) for k, v in st.items()}
    return source_catalogue(configs), st


def _item(row: dict[str, Any], st: SourceAdapterState | None, probed: dict[str, Any] | None = None) -> ExplorerItem:
    h = dict(row["health"])
    hp = probed or {}
    return ExplorerItem(
        id=row["id"], adapter_id=row["adapter_id"], catalogue_id=row["catalogue_id"], name=row["name"], countries=row["countries"],
        source_type=row["source_type"], access_method=row["access_method"], licence=row["licence"], cost=row["cost"],
        credentials=row["credentials"], credentials_required=row["credentials_required"], refresh_frequency=row["refresh_frequency"],
        automated_allowed=row["automated_allowed"], industries=row["industries"], configured=row["configured"],
        enabled=bool(st.enabled) if st else False, info_only=row["info_only"], live_verified=row["live_verified"], url=row["url"] or "",
        health=HealthOut(status=hp.get("status") or h["status"], message=hp.get("message") or h["message"],
                         checked_at=datetime.fromisoformat(hp["checked_at"]) if hp.get("checked_at") else None, probed=bool(hp)),
        score=row.get("score"), score_breakdown=row.get("score_breakdown") or {},
    )


def _find(rows: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    for r in rows:
        if sid in (r["id"], r["adapter_id"], r["catalogue_id"]):
            return r
    raise ApiError(404, "not_found", f"Unknown source '{sid[:80]}'.")


def _accepts_upload(row: dict[str, Any]) -> bool:
    if not row["adapter_id"] or row["info_only"] or not row["automated_allowed"]:
        return False
    try:
        info = get_adapter(row["adapter_id"]).info
    except KeyError:
        return False
    return info.access_method in ("bulk_file", "manual_import") or "path" in (info.access_requirements or "").lower()


def _permission(row: dict[str, Any], cfg: dict[str, Any]) -> PermissionOut:
    basis, note = cfg.get("permission_basis"), cfg.get("permission_note")
    if row["info_only"] and row["adapter_id"] is None or (row["adapter_id"] and row["info_only"]):
        return PermissionOut(status="not_automatable", basis=basis, note=note, can_enable=False,
                             reason="Catalogue entry only: no automated adapter exists for this source (use its official bulk file or licensed feed).")
    if not row["automated_allowed"]:
        return PermissionOut(status="restricted", basis=basis, note=note, can_enable=False,
                             reason="The source's terms do not permit automated collection; it cannot be enabled.")
    if basis in PERMISSION_BASES:
        return PermissionOut(status="permission_declared", basis=basis, note=note, can_enable=True,
                             reason=f"Operator declared the permission basis '{basis}'.")
    if row["credentials_required"] or row["cost"] == "paid":
        return PermissionOut(status="permission_needed", basis=None, note=note, can_enable=False,
                             reason="This source needs credentials or a paid licence: configure it and declare the permission basis first.")
    return PermissionOut(status="open_licence", basis=basis, note=note, can_enable=True, reason="Open data; automated collection is permitted by its licence.")


async def _detail(session: AsyncSession, sid: str) -> ExplorerDetail:
    rows, states = await _catalogue(session)
    row = _find(rows, sid)
    aid = row["adapter_id"] or row["id"]
    st = states.get(aid)
    cfg = dict(st.config or {}) if st else {}
    probed = await get_setting(session, f"source_health.{aid}", None)
    item = _item(row, st, probed)
    info = None
    if row["adapter_id"]:
        try:
            info = get_adapter(row["adapter_id"]).info
        except KeyError:
            info = None
    req = RequirementsOut(
        requires_config=bool(info and info.requires_config), credentials=row["credentials"], credentials_note=row["credentials_note"],
        access_requirements=(info.access_requirements if info else row["notes"]) or "", accepts_file_upload=_accepts_upload(row),
        config_keys_set=sorted(cfg), secret_keys_set=sc.secret_keys(cfg), permitted_permission_bases=list(PERMISSION_BASES))
    stt = StateOut(enabled=bool(st.enabled) if st else False, priority=st.priority if st else None,
                   last_run_at=st.last_run_at if st else None, last_success_at=st.last_success_at if st else None,
                   runs_ok=st.runs_ok if st else 0, runs_failed=st.runs_failed if st else 0,
                   businesses_recovered=st.businesses_recovered if st else 0, last_error=st.last_error if st else None)
    rp = {}
    if info:
        rp = {"requests_per_minute": info.rate_policy.requests_per_minute, "concurrency": info.rate_policy.concurrency}
    return ExplorerDetail(**item.model_dump(), notes=row["notes"] or "", usage=row["usage"] or "", automated_extraction=row.get("automated_extraction"),
                          requirements=req, permission=_permission(row, cfg), state=stt, rate_policy=rp, config=sc.mask_config(cfg))


def _cursor(score: float, sid: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([score, sid]).encode()).decode().rstrip("=")


def _decode_cursor(c: str) -> tuple[float, str]:
    try:
        s, i = json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))
        return float(s), str(i)
    except (ValueError, TypeError, binascii.Error):
        raise ApiError(422, "invalid_cursor", "The pagination cursor is invalid. Start again from the first page.") from None


# --------------------------------------------------------------------------- routes
@router.get("", response_model=Page[ExplorerItem])
async def search_sources(
    country: str | None = Query(None, min_length=2, max_length=2, description="ISO alpha-2 country the source must cover"),
    business_type: str | None = Query(None, max_length=100, description="industry / business type the source should serve"),
    access_method: str | None = Query(None, description="bulk_file | api | manual_import | restricted"),
    licence: str | None = Query(None, max_length=100, description="licence substring"),
    cost: str | None = Query(None, description="free | free_registration | paid"),
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None, session: AsyncSession = Depends(get_session),
) -> Page[ExplorerItem]:
    """Deterministic ranked search; every result carries its ``score_breakdown`` (see ``catalogue_full.SCORE_WEIGHTS``)."""
    check_enum(access_method, ACCESS_METHODS, "access_method")
    check_enum(cost, ("free", "free_registration", "paid"), "cost")
    rows, states = await _catalogue(session)
    hits = search_catalogue(country=country, business_type=clean_q(business_type), access_method=access_method, licence=clean_q(licence),
                            cost=cost, q=clean_q(q), rows=rows)
    if cursor:
        s0, i0 = _decode_cursor(cursor)
        hits = [h for h in hits if h["score"] < s0 or (h["score"] == s0 and h["id"] > i0)]
    total = len(hits)
    page = hits[:limit]
    probes = {}
    for h in page:
        probes[h["id"]] = await get_setting(session, f"source_health.{h['adapter_id']}", None) if h["adapter_id"] else None
    items = [_item(h, states.get(h["adapter_id"] or h["id"]), probes.get(h["id"])) for h in page]
    nxt = _cursor(page[-1]["score"], page[-1]["id"]) if len(hits) > limit and page else None
    return Page[ExplorerItem](items=items, next_cursor=nxt, total_estimate=total if not cursor else None, approximate=False)


@router.get("/{source_id}", response_model=ExplorerDetail)
async def get_source_detail(source_id: str, session: AsyncSession = Depends(get_session)) -> ExplorerDetail:
    return await _detail(session, source_id)


def _validate_config(body: ConfigureIn, up: Path, old: dict[str, Any]) -> dict[str, Any]:
    new = dict(old)
    for k in body.remove_keys:
        new.pop(k, None)
    if body.permission_basis is not None:
        if body.permission_basis not in PERMISSION_BASES:
            raise ApiError(422, "invalid_permission_basis", f"permission_basis must be one of: {', '.join(PERMISSION_BASES)}.")
        new["permission_basis"] = body.permission_basis
    if body.permission_note is not None:
        new["permission_note"] = body.permission_note.strip()[:500]
    for k, v in body.config.items():
        if not KEY_RE.match(k):
            raise ApiError(422, "invalid_config_key", f"'{k[:60]}' is not a valid configuration key (lower-case letters, digits, underscore).")
        if k in ("permission_basis", "permission_note"):
            raise ApiError(422, "invalid_config_key", f"Use the dedicated '{k}' field.")
        if isinstance(v, dict) or (isinstance(v, list) and (len(v) > 100 or any(isinstance(x, dict | list) for x in v))):
            raise ApiError(422, "invalid_config_value", f"'{k}' must be a scalar or a short list of scalars.")
        if isinstance(v, str) and len(v) > 2000:
            raise ApiError(422, "invalid_config_value", f"'{k}' is too long.")
        if v == sc.MASK and k in old:  # unchanged masked secret echoed back by the client
            continue
        if k.lower() in PATH_KEYS:
            if not isinstance(v, str):
                raise ApiError(422, "invalid_path", f"'{k}' must be a path string.")
            try:
                ok = Path(v).resolve().is_relative_to(up)
            except (OSError, ValueError):
                ok = False
            if not ok:
                raise ApiError(422, "invalid_path", f"'{k}' must point to a file uploaded through the Source Explorer.")
        if k.lower().endswith("url") and isinstance(v, str) and v and not re.match(r"^https?://", v, re.I):
            raise ApiError(422, "invalid_config_value", f"'{k}' must be an http(s) URL.")
        if sc.SECRET_KEY_RE.search(k) and isinstance(v, str) and v:
            new[k] = sc.seal_value(v)
        else:
            new[k] = v
    if len(new) > MAX_KEYS or len(json.dumps(new)) > MAX_CONFIG_BYTES:
        raise ApiError(413, "payload_too_large", "Source configuration is limited to 30 keys / 16 KB.")
    return new


@router.post("/{source_id}/configure", response_model=ExplorerDetail)
async def configure_source(source_id: str, body: ConfigureIn, request: Request, user: User = Depends(require_role("operator")),
                           session: AsyncSession = Depends(get_session)) -> ExplorerDetail:
    rows, states = await _catalogue(session)
    row = _find(rows, source_id)
    aid = row["adapter_id"] or row["id"]
    st = states.get(aid)
    if st is None:
        info = get_adapter(row["adapter_id"]).info if row["adapter_id"] else None
        st = SourceAdapterState(adapter_id=aid, enabled=False, priority=info.default_priority if info else 100, config={})
        session.add(st)
        await session.flush()
    before = sc.mask_config(st.config)
    st.config = _validate_config(body, uploads_dir(app_settings(request)), dict(st.config or {}))
    await audit(session, user, "source_explorer.configure", "source", aid,
                {"before": before, "after": sc.mask_config(st.config), "secret_keys": sc.secret_keys(st.config)})
    await session.commit()
    return await _detail(session, aid)


async def _toggle(session: AsyncSession, user: User, source_id: str, enable: bool) -> ToggleOut:
    rows, states = await _catalogue(session)
    row = _find(rows, source_id)
    aid = row["adapter_id"] or row["id"]
    st = states.get(aid)
    cfg = dict(st.config or {}) if st else {}
    if enable:
        perm = _permission(row, cfg)
        if not perm.can_enable:
            raise ApiError(409, "not_permitted", perm.reason)
        if row["adapter_id"] and get_adapter(row["adapter_id"]).info.requires_config and not cfg:
            raise ApiError(409, "not_configured", "This source needs configuration (credentials / dataset) before it can be enabled.")
    if st is None:
        if not enable:
            return ToggleOut(adapter_id=aid, enabled=False)
        info = get_adapter(row["adapter_id"]).info
        st = SourceAdapterState(adapter_id=aid, enabled=False, priority=info.default_priority, config={})
        session.add(st)
    before = bool(st.enabled)
    st.enabled = enable
    await audit(session, user, "source_explorer.enable" if enable else "source_explorer.disable", "source", aid, {"before": before, "after": enable})
    await session.commit()
    return ToggleOut(adapter_id=aid, enabled=enable)


@router.post("/{source_id}/enable", response_model=ToggleOut)
async def enable_explorer_source(source_id: str, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ToggleOut:
    return await _toggle(session, user, source_id, True)


@router.post("/{source_id}/disable", response_model=ToggleOut)
async def disable_explorer_source(source_id: str, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ToggleOut:
    return await _toggle(session, user, source_id, False)


def _sniff_dataset(data: bytes) -> str:
    """'zip' | 'gz' | 'bz2' | 'xlsx' | 'text' or a 422. Rejects executables, HTML, PDF, legacy OLE and binary blobs."""
    head = data[:8]
    bad = (b"MZ", b"\x7fELF", b"%PDF", b"Rar!", b"7z\xbc\xaf", b"\xca\xfe\xba\xbe", b"#!", b"<?php", b"<html", b"<!DOC", b"<script", b"\xd0\xcf\x11\xe0")
    if any(data.lstrip()[:12].lower().startswith(m.lower()) for m in bad) or any(data.startswith(m) for m in bad):
        raise ApiError(422, "unsupported_file", "This file looks like an executable, document or web page, not a dataset.")
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if len(infos) > 200:
                    raise ApiError(422, "unsupported_file", "The archive has too many members.")
                total = sum(i.file_size for i in infos)
                if total > 3 * 1024**3 or (total > 50 * 1024**2 and total / max(1, len(data)) > 300):
                    raise ApiError(422, "unsupported_file", "The archive expands to an unreasonable size.")
                if any(i.filename.startswith(("/", "..")) or ".." in i.filename.split("/") for i in infos):
                    raise ApiError(422, "unsupported_file", "The archive contains unsafe member names.")
                if any(i.filename.lower().endswith((".exe", ".dll", ".js", ".sh", ".bat", ".vbs", "vbaproject.bin")) for i in infos):
                    raise ApiError(422, "unsupported_file", "The archive contains executable members.")
                names = z.namelist()
        except zipfile.BadZipFile:
            raise ApiError(422, "unsupported_file", "That file is not a valid zip archive.") from None
        return "xlsx" if "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names) else "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    sample = data[:65536]
    if b"\x00" in sample:
        raise ApiError(422, "unsupported_file", "This file contains binary data and is not a text dataset (CSV / JSON / XML).")
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        if e.start < len(sample) - 4:
            raise ApiError(422, "unsupported_file", "The file is not valid UTF-8 text. Save it as UTF-8 and try again.") from None
    return "text"


@router.post("/{source_id}/import", response_model=ImportOut, status_code=201)
async def import_source_file(source_id: str, request: Request, file: UploadFile = File(...), user: User = Depends(require_role("operator")),
                             session: AsyncSession = Depends(get_session)) -> ImportOut:
    """Upload a dataset file for a file-based adapter; its stored path becomes the adapter's ``path`` configuration."""
    rows, states = await _catalogue(session)
    row = _find(rows, source_id)
    if not _accepts_upload(row):
        raise ApiError(409, "upload_not_supported", "This source does not take an uploaded dataset file.")
    settings = app_settings(request)
    data = await read_limited(file, settings.max_upload_mb * 1024 * 1024)
    kind = _sniff_dataset(data)
    d = uploads_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    uid = secrets.token_hex(16)
    path = d / f"{uid}.{IMPORT_EXTS.get(kind, kind)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    aid = row["adapter_id"]
    st = states.get(aid)
    if st is None:
        st = SourceAdapterState(adapter_id=aid, enabled=False, priority=get_adapter(aid).info.default_priority, config={})
        session.add(st)
        await session.flush()
    before = dict(st.config or {}).get("path")
    st.config = {**(st.config or {}), "path": str(path)}
    await audit(session, user, "source_explorer.import", "source", aid, {"upload_id": uid, "kind": kind, "size": len(data), "before_path": before})
    await session.commit()
    return ImportOut(adapter_id=aid, upload_id=uid, kind=IMPORT_EXTS.get(kind, kind), size=len(data), path=str(path))


_ = (list_adapters, ACCESS_METHODS)
