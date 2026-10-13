"""Tolerant access to optional sibling modules (source registry, geo, taxonomy, verification pipeline)."""
from __future__ import annotations

import importlib
import logging
from typing import Any

from ..security import ApiError

log = logging.getLogger("api.registry")


def optional(module: str) -> Any | None:
    try:
        return importlib.import_module(module)
    except ImportError:
        return None
    except Exception:  # a sibling module that is half-written must not take the API down
        log.warning("optional module %s failed to import", module, exc_info=True)
        return None


def require(module: str, feature: str) -> Any:
    m = optional(module)
    if m is None:
        raise ApiError(501, "not_available", f"{feature} is not available in this deployment (module {module} is not installed).")
    return m


def adapter_catalog() -> dict[str, dict[str, Any]] | None:
    """adapter_id -> info dict from the source registry, or None when the registry cannot be loaded."""
    base = optional("app.sources.base")
    if base is None:
        return None
    try:
        out: dict[str, dict[str, Any]] = {}
        for a in base.list_adapters():
            d = a.info.to_dict()
            out[d["adapter_id"]] = d
        return out
    except Exception as e:  # e.g. a registered module that is missing
        log.warning("source registry unavailable: %s", e)
        return None
