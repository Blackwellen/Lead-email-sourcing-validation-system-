"""Directory / register CATALOGUE -- data only, never a scraper.

``app/data/directory_catalogue.json`` lists candidate directories, registers and open datasets per country with a
best-knowledge ``automated_extraction`` status.  It is NOT legal advice and terms change: the operator MUST
re-verify each entry's current terms before enabling any automated access.  Default policy: deny.

Every catalogue entry without a real adapter is exposed in the adapter registry as an info-only adapter
(``dir_<id>``) whose ``iter_businesses`` raises :class:`NotPermitted` (or :class:`AdapterUnavailable` for open
datasets that simply are not implemented yet).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from .base import (
    AdapterHealth,
    AdapterInfo,
    AdapterUnavailable,
    BusinessRecord,
    Checkpoint,
    DiscoveryQuery,
    NotPermitted,
    RatePolicy,
    SourceAdapter,
    register,
)

CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "directory_catalogue.json"
STATUSES = ("prohibited", "permission_required", "api_available", "unknown_review_terms", "allowed_open_data")
ACCESS_PATHS = ("manual domain discovery", "licensed feed", "official API", "bulk dataset", "skip")
_KIND_TO_SOURCE_TYPE = {
    "directory": "directory", "association": "directory", "register": "registry",
    "bulk_dataset": "bulk_dataset", "open_data": "open_data",
}
DISCLAIMER_FALLBACK = "Terms must be re-verified by the operator."


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    with open(CATALOGUE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_catalogue(path: Path | None = None) -> list[dict[str, Any]]:
    """All catalogue entries (validated)."""
    data = json.load(open(path, encoding="utf-8")) if path else _load_raw()
    entries = data["entries"]
    seen: set[str] = set()
    for e in entries:
        if e["id"] in seen:
            raise ValueError(f"duplicate catalogue id {e['id']}")
        seen.add(e["id"])
        if e["automated_extraction"] not in STATUSES:
            raise ValueError(f"bad status for {e['id']}")
        if e["recommended_access"] not in ACCESS_PATHS:
            raise ValueError(f"bad access path for {e['id']}")
    return entries


def catalogue_disclaimer() -> str:
    return _load_raw().get("disclaimer", DISCLAIMER_FALLBACK)


def entries_for_country(country_code: str) -> list[dict[str, Any]]:
    cc = country_code.upper()
    return [e for e in load_catalogue() if "*" in e["countries"] or cc in e["countries"]]


def get_entry(entry_id: str) -> dict[str, Any]:
    for e in load_catalogue():
        if e["id"] == entry_id:
            return e
    raise KeyError(entry_id)


def is_automation_permitted(entry: dict[str, Any]) -> bool:
    """Default-deny: only open data (and nothing else) is considered permitted, and even then re-verify."""
    return entry["automated_extraction"] == "allowed_open_data"


class CatalogueEntryAdapter(SourceAdapter):
    entry: ClassVar[dict[str, Any]]

    async def health(self) -> AdapterHealth:
        return AdapterHealth(False, "info_only", f"catalogue entry only ({self.entry['automated_extraction']}). {DISCLAIMER_FALLBACK}")

    async def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        e = self.entry
        if e["automated_extraction"] == "allowed_open_data" and e["adapter_id"] is None:
            raise AdapterUnavailable(f"{e['name']}: open dataset listed in the catalogue but no adapter is implemented yet")
        raise NotPermitted(
            f"{e['name']}: automated extraction is '{e['automated_extraction']}'. Recommended access: "
            f"{e['recommended_access']}. {e['licence_notes']} {DISCLAIMER_FALLBACK}"
        )
        yield  # pragma: no cover  (makes this an async generator)


def _register_entries() -> None:
    for e in load_catalogue():
        if e.get("adapter_id"):
            continue  # backed by a real adapter registered elsewhere
        countries = tuple(e["countries"]) if e["countries"] else ("*",)
        info = AdapterInfo(
            adapter_id=f"dir_{e['id']}",
            name=e["name"],
            country_coverage=countries,
            source_type=_KIND_TO_SOURCE_TYPE.get(e.get("kind", "directory"), "directory"),
            licence=e["licence_notes"][:200],
            source_url=e["url"],
            access_requirements=f"Catalogue entry; automated_extraction={e['automated_extraction']}; recommended: {e['recommended_access']}",
            automated_collection_permitted=False,
            rate_policy=RatePolicy(None),
            info_only=True,
            notes=e["licence_notes"] + " " + DISCLAIMER_FALLBACK,
        )
        cls = type(f"Dir_{e['id']}", (CatalogueEntryAdapter,), {"info": info, "entry": e})
        register(cls)


_register_entries()

__all__ = [
    "load_catalogue", "entries_for_country", "get_entry", "is_automation_permitted", "catalogue_disclaimer",
    "CatalogueEntryAdapter",
]
