"""Single policy for user-supplied source configuration (adapter config, run ``source_params``, monitor config).

Used by every API that accepts source configuration AND by the worker right before it instantiates an adapter
(:func:`assert_adapter_runnable`), so a value that slipped past one entry point (an old draft, a direct DB edit, a
scheduled run) is still refused at execution time.

Rules
-----
* **Path-like keys** (:func:`is_path_key`): ``path``/``paths``/``file``/``filename``/``upload`` and anything ending in
  ``_path``/``_paths``/``_file``/``_files``/``_index``/``_dir``. String values AND list elements must resolve
  (``realpath``: symlinks and ``..`` followed) to a file *inside* ``data_dir/uploads``.
* **Write/working locations** (:func:`is_forbidden_key`): ``*_dir`` (``data_dir``, ``spill_dir``, ``work_dir`` ...),
  ``store_index``/``keep_index``, ``spill``/``work`` are never accepted from user configuration. Adapters derive these
  from settings.
* **URL keys** (:func:`is_url_key`, ``url``/``*_url``/``urls``/``*_urls``): checked with the crawler SSRF ``Guard``
  (scheme, credentials, port allow-list, private / loopback / metadata IP literals, local host names).
  ``settings.allow_private_networks`` relaxes only the address/port part (tests), as everywhere else.
* ``permission_basis`` / ``permission_note`` are only meaningful in the admin-managed adapter state; they are refused
  in run ``source_params``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from .base import NotPermitted

PATH_KEYS = frozenset({"path", "paths", "file", "files", "filename", "upload", "uploads"})
PATH_SUFFIXES = ("_path", "_paths", "_file", "_files", "_index", "_dir", "_dirs")
FORBIDDEN_KEYS = frozenset({"store_index", "keep_index", "spill", "work", "dir", "directory", "data_dir", "spill_dir", "work_dir", "cache_dir"})
FORBIDDEN_SUFFIXES = ("_dir", "_dirs", "_directory")
PERMISSION_KEYS = frozenset({"permission_basis", "permission_note", "permission_basis_by"})
PERMISSION_BASES = ("open_licence", "licence_agreement", "api_terms", "written_permission", "public_domain")


def is_path_key(key: Any) -> bool:
    k = str(key).lower()
    return k in PATH_KEYS or k.endswith(PATH_SUFFIXES) or k in FORBIDDEN_KEYS


def is_forbidden_key(key: Any) -> bool:
    k = str(key).lower()
    return k in FORBIDDEN_KEYS or k.endswith(FORBIDDEN_SUFFIXES)


def is_url_key(key: Any) -> bool:
    k = str(key).lower()
    return k in ("url", "urls", "endpoint") or k.endswith(("_url", "_urls", "url"))


def uploads_root(settings: Settings | None = None) -> Path:
    st = settings or get_settings()
    return Path(os.path.realpath(Path(st.data_dir) / "uploads"))


def path_inside(value: Any, root: Path) -> bool:
    """True when ``value`` is a string naming something strictly inside ``root`` after resolving symlinks / ``..``."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    try:
        real = Path(os.path.realpath(value))
    except (OSError, ValueError):
        return False
    return real != root and real.is_relative_to(root)


def _walk(params: Any, prefix: str) -> Iterator[tuple[str, str, Any]]:
    """Yield (field, key, value) for every dict entry, recursively (lists of dicts included)."""
    if isinstance(params, dict):
        for k, v in params.items():
            yield f"{prefix}{k}", str(k), v
            if isinstance(v, dict | list) and not is_path_key(k) and not is_url_key(k):
                yield from _walk(v, f"{prefix}{k}.")
    elif isinstance(params, list):
        for i, v in enumerate(params):
            if isinstance(v, dict | list):
                yield from _walk(v, f"{prefix}{i}.")


def check_url(value: Any, settings: Settings | None = None) -> str | None:
    """Return a problem description, or None when the URL passes the SSRF guard's static checks."""
    from ..crawler.safe_http import Guard, SSRFBlocked

    st = settings or get_settings()
    if not isinstance(value, str):
        return "must be an http(s) URL string"
    if not value.strip():
        return None
    try:
        Guard(allow_private=lambda: bool(st.allow_private_networks)).check_url(value)
    except SSRFBlocked as e:
        return f"is not an allowed address ({e})"
    except ValueError as e:
        return f"is not a valid URL ({e})"
    return None


def config_problems(params: Any, settings: Settings | None = None, *, prefix: str = "", allow_permission_keys: bool = False) -> list[tuple[str, str]]:
    """All policy violations in a (nested) configuration mapping as ``(field, message)`` pairs."""
    st = settings or get_settings()
    root = uploads_root(st)
    out: list[tuple[str, str]] = []
    if params is None:
        return out
    if not isinstance(params, dict):
        return [(prefix.rstrip(".") or "config", "must be an object")]
    for field, key, v in _walk(params, prefix):
        if key.lower() in PERMISSION_KEYS and not allow_permission_keys:
            out.append((field, "permission settings can only be declared by an administrator on the Sources page"))
        elif is_forbidden_key(key):
            out.append((field, "server directories / index stores are derived from the server settings and cannot be configured"))
        elif is_path_key(key):
            vals = v if isinstance(v, list) else [v]
            if v is None or v == "":
                continue
            if not vals or any(not path_inside(x, root) for x in vals):
                out.append((field, "Files must be uploaded through the Sources page first; arbitrary server paths are not allowed."))
        elif is_url_key(key) and v not in (None, ""):
            for x in (v if isinstance(v, list) else [v]):
                problem = check_url(x, st)
                if problem:
                    out.append((field, f"'{str(x)[:80]}' {problem}"))
                    break
    return out


async def assert_adapter_runnable(session: Any, adapter_id: str, params: dict[str, Any] | None, settings: Settings | None = None) -> None:
    """Raise :class:`~app.sources.base.NotPermitted` unless ``adapter_id`` may run with ``params`` right now.

    * the adapter exists, is not ``info_only`` and is not explicitly disabled in ``source_adapter_state``;
    * it permits automated collection, or an administrator declared a permission basis in the stored (admin-only)
      adapter configuration;
    * every path-like value in ``params`` lies inside the uploads directory, no server directory / index-store key is
      present, and every URL passes the SSRF guard.

    ``params`` is the merged configuration the worker is about to hand to the adapter (stored config + run
    ``source_params`` + job params). Call it with the session the job runs in.
    """
    from ..models import SourceAdapterState
    from .base import get_adapter

    try:
        info = get_adapter(adapter_id).info
    except KeyError:
        raise NotPermitted(f"unknown source '{adapter_id}'") from None
    if info.info_only:
        raise NotPermitted(f"source '{adapter_id}' is a catalogue entry only and cannot be run")
    st = await session.get(SourceAdapterState, adapter_id)
    if st is not None and not st.enabled:
        raise NotPermitted(f"source '{adapter_id}' is disabled")
    stored = dict(st.config or {}) if st is not None else {}
    if not info.automated_collection_permitted:
        if not (stored.get("permission_basis") in PERMISSION_BASES and stored.get("permission_basis_by")):
            raise NotPermitted(f"source '{adapter_id}' does not permit automated collection and no administrator declared a permission basis")
    run_params = {k: v for k, v in (params or {}).items() if k not in PERMISSION_KEYS or stored.get(k) == v}
    problems = config_problems(run_params, settings, allow_permission_keys=True)
    if problems:
        field, msg = problems[0]
        raise NotPermitted(f"source '{adapter_id}' configuration refused: {field}: {msg}")
