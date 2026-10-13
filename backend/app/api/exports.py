"""Export jobs: presets, create, history, download, delete."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..exports import (
    COLUMNS,
    FORMATS,
    PRESETS,
    ExportFilters,
    apply_preset,
    create_export,
    estimate_rows,
    header_for,
    resolve_export_path,
)
from ..models import Export, User
from ..security import ApiError, audit, require_role
from .common import Ok, Page, SortSpec, apply_keyset, check_enum, finish_page

log = logging.getLogger("api.exports")
router = APIRouter(prefix="/api/exports", tags=["exports"], dependencies=[Depends(require_role("viewer"))])

MEDIA = {"csv": "text/csv; charset=utf-8", "jsonl": "application/x-ndjson", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


class ExportOut(BaseModel):
    id: int
    created_by: int | None
    created_at: datetime
    preset: str
    format: str
    filters: dict[str, Any]
    status: str
    row_count: int
    file_size: int | None = None
    error: str | None = None
    run_id: int | None = None
    download_url: str | None = None


class PresetInfo(BaseModel):
    id: str
    label: str
    description: str
    columns: list[str]


class PresetList(BaseModel):
    presets: list[PresetInfo]
    formats: list[str]
    required_columns: list[str]


class ExportCreateIn(BaseModel):
    preset: str = "full_intelligence"
    format: Literal["csv", "xlsx", "jsonl"] = "csv"
    filters: ExportFilters = Field(default_factory=ExportFilters)
    run_id: int | None = None
    wait: bool | None = Field(None, description="true = run inline now, false = always queue, null = inline only for small exports")


class PreviewOut(BaseModel):
    estimated_rows: int | None
    capped: bool
    columns: list[str]


def out(ex: Export) -> ExportOut:
    return ExportOut(
        id=ex.id, created_by=ex.created_by, created_at=ex.created_at, preset=ex.preset, format=ex.format, filters=ex.filters or {}, status=ex.status,
        row_count=ex.row_count, file_size=ex.file_size, error=ex.error, run_id=ex.run_id,
        download_url=f"/api/exports/{ex.id}/download" if ex.status == "done" and ex.file_path else None,
    )


def _can_see(user: User, ex: Export) -> bool:
    return user.role == "admin" or ex.created_by == user.id


async def _owned(session: AsyncSession, export_id: int, user: User) -> Export:
    ex = await session.get(Export, export_id)
    if ex is None:
        raise ApiError(404, "not_found", "Export not found.")
    if not _can_see(user, ex):
        raise ApiError(403, "forbidden", "You can only access exports that you created.")
    return ex


@router.get("/presets", response_model=PresetList)
async def list_export_presets() -> PresetList:
    return PresetList(
        presets=[PresetInfo(id=k, label=v["label"], description=v["description"], columns=header_for(k)) for k, v in PRESETS.items()],
        formats=list(FORMATS), required_columns=COLUMNS,
    )


@router.post("/preview", response_model=PreviewOut, dependencies=[Depends(require_role("operator"))])
async def preview_export(body: ExportCreateIn, session: AsyncSession = Depends(get_session)) -> PreviewOut:
    f = apply_preset(body.preset, body.filters)
    n, capped = await estimate_rows(session, f)
    return PreviewOut(estimated_rows=n, capped=capped, columns=header_for(body.preset))


@router.get("", response_model=Page[ExportOut])
async def list_exports(
    status: str | None = None, preset: str | None = Query(None, max_length=40), limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    user: User = Depends(require_role("viewer")), session: AsyncSession = Depends(get_session),
) -> Page[ExportOut]:
    check_enum(status, {"queued", "running", "done", "failed"}, "status")
    sorts = {"id": SortSpec(Export.id, "int")}
    stmt = select(Export, Export.id.label("_sv"), Export.id.label("_id"))
    if user.role != "admin":
        stmt = stmt.where(Export.created_by == user.id)
    if status:
        stmt = stmt.where(Export.status == status)
    if preset:
        stmt = stmt.where(Export.preset == preset)
    stmt = apply_keyset(stmt, sorts, "id", "desc", Export.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    return Page[ExportOut](items=[out(r[0]) for r in rows], next_cursor=nxt)


@router.post("", response_model=ExportOut, status_code=201)
async def create_export_job(body: ExportCreateIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> ExportOut:
    ex = await create_export(session, user=user, preset=body.preset, fmt=body.format, filters=body.filters, run_id=body.run_id, run_inline=body.wait)
    await audit(session, user, "export.create", "export", ex.id, {"preset": body.preset, "format": ex.format, "status": ex.status, "rows": ex.row_count, "filters": body.filters.model_dump(mode="json", exclude_defaults=True, exclude={"email_ids", "organisation_ids"})})
    await session.commit()
    return out(ex)


@router.get("/{export_id}", response_model=ExportOut)
async def get_export(export_id: int, user: User = Depends(require_role("viewer")), session: AsyncSession = Depends(get_session)) -> ExportOut:
    return out(await _owned(session, export_id, user))


@router.get("/{export_id}/download", response_class=FileResponse, responses={200: {"content": {"application/octet-stream": {}}, "description": "The export file"}})
async def download_export(export_id: int, user: User = Depends(require_role("viewer")), session: AsyncSession = Depends(get_session)):
    ex = await _owned(session, export_id, user)
    if ex.status != "done" or not ex.file_path:
        raise ApiError(409, "export_not_ready", f"This export is {ex.status}; it can be downloaded once it is done.")
    path = resolve_export_path(ex.file_path)
    if path is None:
        raise ApiError(410, "export_file_missing", "The export file is no longer available (it may have been removed by the retention policy). Create the export again.")
    await audit(session, user, "export.download", "export", ex.id, {"rows": ex.row_count})
    await session.commit()
    name = f"export-{ex.id}-{ex.preset}.{ex.format}"
    return FileResponse(path, media_type=MEDIA[ex.format], filename=name, headers={"Cache-Control": "private, no-store"})


@router.delete("/{export_id}", response_model=Ok)
async def delete_export(export_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> Ok:
    ex = await _owned(session, export_id, user)
    if ex.status == "running":
        raise ApiError(409, "export_running", "This export is still running; wait for it to finish.")
    p = resolve_export_path(ex.file_path)
    if p:
        p.unlink(missing_ok=True)
    await audit(session, user, "export.delete", "export", ex.id, {"preset": ex.preset})
    await session.delete(ex)
    await session.commit()
    return Ok(message="Export deleted.")
