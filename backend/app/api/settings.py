"""Admin settings sections, audit-log viewer and the retention job trigger."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import AuditEvent, User
from ..retention import apply_retention
from ..security import ApiError, audit, require_role
from ..settings_schema import (
    ADVANCED,
    SECTIONS,
    BackupSettings,
    ConcurrencySettings,
    ExportSettings,
    NotificationSettings,
    RetentionSettings,
    SourcePolicySettings,
    ValidationSettings,
    WorkerSettings,
    load_section,
    save_section,
)
from .common import Page, SortSpec, apply_keyset, finish_page

router = APIRouter(prefix="/api", tags=["settings"], dependencies=[Depends(require_role("viewer"))])


class AllSettings(BaseModel):
    """Sections the caller may see: advanced ones (worker, concurrency, retention, backups, source_policies) are admin-only (null otherwise)."""

    worker: WorkerSettings | None = None
    source_policies: SourcePolicySettings | None = None
    validation: ValidationSettings | None = None
    concurrency: ConcurrencySettings | None = None
    retention: RetentionSettings | None = None
    exports: ExportSettings | None = None
    notifications: NotificationSettings | None = None
    backups: BackupSettings | None = None


class AuditRow(BaseModel):
    id: int
    at: datetime
    user_id: int | None
    user_email: str | None = None
    action: str
    object_type: str | None
    object_id: str | None
    detail: dict[str, Any]


class RetentionResult(BaseModel):
    counts: dict[str, Any]


@router.get("/settings", response_model=AllSettings)
async def get_settings_all(user: User = Depends(require_role("viewer")), session: AsyncSession = Depends(get_session)) -> AllSettings:
    out: dict[str, Any] = {}
    for name in SECTIONS:
        if name in ADVANCED and user.role != "admin":
            continue
        out[name] = await load_section(session, name)
    return AllSettings(**out)


def _make_update(name: str, model: type[BaseModel]):
    async def endpoint(body: model, user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)):  # type: ignore[valid-type]
        await save_section(session, name, body)
        await audit(session, user, "settings.update", "settings", name, {"section": name, "values": body.model_dump(mode="json")})
        await session.commit()
        return body

    endpoint.__annotations__ = {"body": model, "user": User, "session": AsyncSession}
    endpoint.__name__ = f"update_{name}_settings"
    router.add_api_route(f"/settings/{name}", endpoint, methods=["PUT"], response_model=model, summary=f"Update {name} settings (admin)")


for _name, _model in SECTIONS.items():
    _make_update(_name, _model)


@router.get("/settings/{section}", response_model=dict[str, Any])
async def get_settings_section(section: str, user: User = Depends(require_role("viewer")), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ApiError(404, "not_found", f"Unknown settings section '{section}'.")
    if section in ADVANCED and user.role != "admin":
        raise ApiError(403, "forbidden", "This settings section is restricted to administrators.")
    return (await load_section(session, section)).model_dump(mode="json")


@router.post("/settings/retention/run", response_model=RetentionResult)
async def run_retention(user: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> RetentionResult:
    counts = await apply_retention(session)
    await audit(session, user, "settings.retention_run", "settings", "retention", counts)
    await session.commit()
    return RetentionResult(counts=counts)


@router.get("/audit", response_model=Page[AuditRow])
async def list_audit(
    user_id: int | None = None, action: str | None = Query(None, max_length=80, description="exact action, or prefix ending in '*' (e.g. run.*)"),
    object_type: str | None = Query(None, max_length=40), object_id: str | None = Query(None, max_length=64), since: datetime | None = None, until: datetime | None = None,
    limit: int = Query(50, ge=1, le=500), cursor: str | None = None, admin: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session),
) -> Page[AuditRow]:
    from .common import escape_like

    sorts = {"id": SortSpec(AuditEvent.id, "int")}
    stmt = select(AuditEvent, User.email, AuditEvent.id.label("_sv"), AuditEvent.id.label("_id")).outerjoin(User, User.id == AuditEvent.user_id)
    if user_id is not None:
        stmt = stmt.where(AuditEvent.user_id == user_id)
    if action:
        if action.endswith("*"):
            stmt = stmt.where(AuditEvent.action.like(escape_like(action[:-1]) + "%", escape="\\"))
        else:
            stmt = stmt.where(AuditEvent.action == action)
    if object_type:
        stmt = stmt.where(AuditEvent.object_type == object_type)
    if object_id:
        stmt = stmt.where(AuditEvent.object_id == object_id)
    if since:
        stmt = stmt.where(AuditEvent.at >= since)
    if until:
        stmt = stmt.where(AuditEvent.at <= until)
    stmt = apply_keyset(stmt, sorts, "id", "desc", AuditEvent.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(stmt)).all(), "id", "desc", limit)
    return Page[AuditRow](items=[AuditRow(id=a.id, at=a.at, user_id=a.user_id, user_email=em, action=a.action, object_type=a.object_type, object_id=a.object_id, detail=a.detail or {}) for a, em, *_ in rows], next_cursor=nxt)
