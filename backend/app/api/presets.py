"""Saved run presets and saved table views."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Preset, User
from ..security import ApiError, audit, require_role
from ..sources.secret_config import is_secret_key
from .common import Ok

router = APIRouter(prefix="/api/presets", tags=["presets"], dependencies=[Depends(require_role("viewer"))])
Kind = Literal["run", "view_contacts", "view_businesses"]
MAX_CONFIG_BYTES = 64 * 1024


class PresetOut(BaseModel):
    id: int
    kind: str
    name: str
    config: dict[str, Any]
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class PresetIn(BaseModel):
    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class PresetUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None


def _out(p: Preset) -> PresetOut:
    return PresetOut(id=p.id, kind=p.kind, name=p.name, config=_strip_secrets(p.config or {}), created_by=p.created_by, created_at=p.created_at, updated_at=p.updated_at)


def _strip_secrets(obj: Any) -> Any:
    """Presets are readable by every role, so credentials never belong in them (they live in source settings,
    sealed). Secret-looking keys are dropped at any depth before storing."""
    if isinstance(obj, dict):
        return {k: _strip_secrets(v) for k, v in obj.items() if not is_secret_key(k)}
    if isinstance(obj, list):
        return [_strip_secrets(v) for v in obj]
    return obj


def _check_size(cfg: dict[str, Any]) -> None:
    if len(json.dumps(cfg)) > MAX_CONFIG_BYTES:
        raise ApiError(413, "payload_too_large", "The preset configuration is too large (64 KB maximum).")


@router.get("", response_model=list[PresetOut])
async def list_presets(kind: Kind | None = None, limit: int = Query(200, ge=1, le=500), session: AsyncSession = Depends(get_session)) -> list[PresetOut]:
    q = select(Preset).order_by(Preset.kind, Preset.name).limit(limit)
    if kind:
        q = q.where(Preset.kind == kind)
    return [_out(p) for p in (await session.execute(q)).scalars()]


@router.post("", response_model=PresetOut, status_code=201)
async def create_preset(body: PresetIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> PresetOut:
    _check_size(body.config)
    p = Preset(kind=body.kind, name=body.name.strip(), config=_strip_secrets(body.config), created_by=user.id)
    session.add(p)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "name_in_use", f"A {body.kind} preset named '{body.name}' already exists.") from None
    await audit(session, user, "preset.create", "preset", p.id, {"kind": p.kind, "name": p.name})
    await session.commit()
    await session.refresh(p)
    return _out(p)


async def _get_owned(session: AsyncSession, preset_id: int, user: User) -> Preset:
    p = await session.get(Preset, preset_id)
    if p is None:
        raise ApiError(404, "not_found", "Preset not found.")
    if user.role != "admin" and p.created_by != user.id:
        raise ApiError(403, "forbidden", "Only the creator or an administrator can change this preset.")
    return p


@router.get("/{preset_id}", response_model=PresetOut)
async def get_preset(preset_id: int, session: AsyncSession = Depends(get_session)) -> PresetOut:
    p = await session.get(Preset, preset_id)
    if p is None:
        raise ApiError(404, "not_found", "Preset not found.")
    return _out(p)


@router.put("/{preset_id}", response_model=PresetOut)
async def update_preset(preset_id: int, body: PresetUpdateIn, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> PresetOut:
    p = await _get_owned(session, preset_id, user)
    if body.config is not None:
        _check_size(body.config)
        p.config = _strip_secrets(body.config)
    if body.name:
        p.name = body.name.strip()
    try:
        await audit(session, user, "preset.update", "preset", p.id, {"name": p.name})
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "name_in_use", "A preset with that name already exists.") from None
    await session.refresh(p)
    return _out(p)


@router.delete("/{preset_id}", response_model=Ok)
async def delete_preset(preset_id: int, user: User = Depends(require_role("operator")), session: AsyncSession = Depends(get_session)) -> Ok:
    p = await _get_owned(session, preset_id, user)
    await audit(session, user, "preset.delete", "preset", p.id, {"kind": p.kind, "name": p.name})
    await session.delete(p)
    await session.commit()
    return Ok(message="Preset deleted.")
