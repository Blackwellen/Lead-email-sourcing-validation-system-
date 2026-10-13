"""Tiny key/value settings helpers (Setting table) + the maintenance-mode flag used by API and workers."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

_MAINT_TTL_S = 2.0
_maint: dict[str, Any] = {"t": 0.0, "v": None}


async def get_setting(s: AsyncSession, key: str, default: Any = None) -> Any:
    row = await s.get(Setting, key)
    return row.value if row is not None else default


async def set_setting(s: AsyncSession, key: str, value: Any) -> None:
    row = await s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value))
    else:
        row.value = value
    await s.flush()


def invalidate_maintenance_cache() -> None:
    _maint["t"] = 0.0
    _maint["v"] = None


async def maintenance_state(s: AsyncSession) -> dict[str, Any]:
    """Maintenance flag, cached for ~2 s per process (cheap enough to call on every request / job claim)."""
    now = time.monotonic()
    if _maint["v"] is not None and now - _maint["t"] < _MAINT_TTL_S:
        return _maint["v"]
    row = (await s.execute(text("SELECT value FROM settings WHERE key='maintenance'"))).first()
    val = row[0] if row and isinstance(row[0], dict) else {}
    val = {"enabled": bool(val.get("enabled")), "message": val.get("message") or "", "since": val.get("since")}
    _maint["t"], _maint["v"] = now, val
    return val


async def maintenance_active(s: AsyncSession) -> bool:
    return bool((await maintenance_state(s))["enabled"])
