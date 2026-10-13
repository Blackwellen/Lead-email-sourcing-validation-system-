"""Source adapter framework: adapter contract, records, queries, registry and DB state sync."""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SourceAdapterState

# --------------------------------------------------------------------------- priority hierarchy
SOURCE_TYPES = (
    "bulk_dataset", "open_data", "registry", "directory", "website", "search_api", "manual_import", "web_archive",
)
# Lower number = more trusted / tried first.  Overridable per adapter via SourceAdapterState.priority.
SOURCE_TYPE_PRIORITY: dict[str, int] = {
    "bulk_dataset": 10,  # official bulk datasets
    "open_data": 20,
    "registry": 30,  # licensed registries
    "website": 40,  # company websites
    "directory": 45,  # info-only unless licensed
    "search_api": 50,
    "web_archive": 55,
    "manual_import": 60,
}


class AdapterError(Exception):
    """Base class for adapter failures."""


class NotPermitted(AdapterError):
    """Automated collection from this source is not permitted (terms/licence); explanation in message."""


class NotConfigured(AdapterError):
    """Adapter needs configuration (path, API key, URL ...) before it can run."""


class AdapterUnavailable(AdapterError):
    """Optional dependency or remote service unavailable."""


# --------------------------------------------------------------------------- value objects
@dataclass(frozen=True)
class RatePolicy:
    requests_per_minute: int | None = None  # None = not applicable (local file)
    concurrency: int = 1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_s: float = 2.0
    retry_on_status: tuple[int, ...] = (500, 502, 503, 504)
    # 429 is NEVER retried in a loop by adapters that must honour quotas (see Brave); it stops the run.
    stop_on_429: bool = True


@dataclass(frozen=True)
class AdapterInfo:
    adapter_id: str
    name: str
    country_coverage: tuple[str, ...]  # alpha-2 codes or ('*',)
    source_type: str
    licence: str
    source_url: str
    access_requirements: str
    automated_collection_permitted: bool
    rate_policy: RatePolicy = field(default_factory=RatePolicy)
    fields_available: tuple[str, ...] = ()
    incremental_support: bool = False
    parser_version: str = "1"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    requires_config: bool = False
    info_only: bool = False  # catalogue / pseudo entries that never yield records here
    supports_radius: bool = False
    attribution: str = ""
    live_verified: bool = False  # True only if live access has actually been exercised
    notes: str = ""
    # --- catalogue metadata (additive; consumed by sources.catalogue_full / the Source Explorer)
    cost: str = "free"  # free | free_registration | paid
    credentials: str = "none"  # none | optional | required
    credentials_note: str = ""
    refresh_frequency: str = ""  # e.g. 'monthly', 'weekly', 'daily', 'continuous', 'static'
    access_method: str = ""  # bulk_file | api | manual_import | restricted ('' = derived by the catalogue)
    industries: tuple[str, ...] = ()  # relevance tags; ('*',) = industry-agnostic
    allowed_usage: str = ""  # one-line statement of what the licence/terms allow

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(f"unknown source_type {self.source_type!r}")

    @property
    def default_priority(self) -> int:
        return SOURCE_TYPE_PRIORITY[self.source_type]

    def covers(self, country_code: str) -> bool:
        return "*" in self.country_coverage or country_code.upper() in self.country_coverage

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        d = asdict(self)
        d["default_priority"] = self.default_priority
        return d


@dataclass
class BusinessRecord:
    source_ref: str
    legal_name: str | None = None
    trading_name: str | None = None
    company_number: str | None = None
    legal_type: str | None = None
    status: str | None = None
    country_code: str = ""
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    email_published: str | None = None  # ONLY if the dataset itself publishes it
    sic_codes: list[str] = field(default_factory=list)
    osm_tags: list[str] = field(default_factory=list)
    category_hint: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # small
    source_url: str | None = None
    licence: str | None = None
    observed_at: datetime | None = None
    adapter_id: str = ""  # stamped by adapters
    attribution: str | None = None


@dataclass
class DiscoveryQuery:
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    localities: list[str] = field(default_factory=list)
    postcodes: list[str] = field(default_factory=list)  # UK: area 'SW', district 'SW1A', sector 'SW1A 1', full
    category_ids: list[int] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    sic_codes: list[str] = field(default_factory=list)
    osm_tags: list[str] = field(default_factory=list)  # 'shop=estate_agent'
    exclusions: list[str] = field(default_factory=list)
    active_only: bool = True
    limit: int | None = None
    centre_lat: float | None = None  # operator input or licensed gazetteer only
    centre_lon: float | None = None
    radius_km: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def with_terms(self, terms: Any) -> DiscoveryQuery:
        """Merge a taxonomy ``DiscoveryQueryTerms`` into this query (union, order-preserving)."""

        def merge(a: list, b: list) -> list:
            return list(dict.fromkeys([*a, *b]))

        return replace(
            self,
            category_ids=merge(self.category_ids, list(terms.category_ids)),
            keywords=merge(self.keywords, list(terms.keywords)),
            synonyms=merge(self.synonyms, list(terms.synonyms)),
            sic_codes=merge(self.sic_codes, list(terms.sic_codes)),
            osm_tags=merge(self.osm_tags, list(terms.osm_tags)),
            exclusions=merge(self.exclusions, list(terms.excluded_keywords)),
        )

    def covers_country(self, cc: str) -> bool:
        return not self.countries or cc.upper() in {c.upper() for c in self.countries}


Checkpoint = dict[str, Any]


@dataclass
class AdapterHealth:
    ok: bool
    status: str  # ok | not_configured | unavailable | info_only | degraded
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "message": self.message, "details": self.details}


# --------------------------------------------------------------------------- adapter ABC
class SourceAdapter(ABC):
    """Contract for every source.

    ``iter_businesses`` yields ``(record, checkpoint)``.  The checkpoint is the state *after* that record:
    passing it back as ``checkpoint=`` resumes with the next record -- no duplicates, no gaps.  Workers
    should persist the latest checkpoint they have fully processed (e.g. every N records).
    Adapters honour ``query.limit`` (max records emitted by one call) and must stream: never load a whole
    dataset in memory.
    """

    info: ClassVar[AdapterInfo]

    def __init__(self, config: dict[str, Any] | None = None):
        self.config: dict[str, Any] = dict(config or {})

    @abstractmethod
    def iter_businesses(
        self, query: DiscoveryQuery, checkpoint: Checkpoint | None = None
    ) -> AsyncIterator[tuple[BusinessRecord, Checkpoint]]:
        """Async generator of ``(record, checkpoint)``."""

    async def health(self) -> AdapterHealth:
        return AdapterHealth(ok=True, status="ok")

    # convenience -----------------------------------------------------------
    @property
    def adapter_id(self) -> str:
        return self.info.adapter_id

    def stamp(self, rec: BusinessRecord) -> BusinessRecord:
        rec.adapter_id = self.info.adapter_id
        if rec.licence is None:
            rec.licence = self.info.licence
        if rec.attribution is None and self.info.attribution:
            rec.attribution = self.info.attribution
        if rec.observed_at is None:
            rec.observed_at = datetime.now(UTC)
        return rec


# --------------------------------------------------------------------------- registry
_REGISTRY: dict[str, type[SourceAdapter]] = {}

# modules whose import registers adapters (kept explicit: no dynamic plugin loading)
ADAPTER_MODULES = (
    "companies_house_bulk", "osm_geofabrik", "csv_import", "wikidata", "search_providers",
    "directory_catalogue", "pseudo",
    # discovery sources (DISC-01..08)
    "charity_commission", "ocds", "gleif", "eu_ted", "usaspending", "abn_lookup", "commoncrawl", "industry_directories",
    "exhibitors", "new_registrations",
)
_loaded = False


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    """Class decorator / function registering an adapter class under ``cls.info.adapter_id``."""
    aid = cls.info.adapter_id
    existing = _REGISTRY.get(aid)
    if existing is not None and existing is not cls and existing.__module__ != cls.__module__:
        raise ValueError(f"duplicate adapter id {aid!r}")
    _REGISTRY[aid] = cls
    return cls


def ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    for m in ADAPTER_MODULES:
        importlib.import_module(f"{__package__}.{m}")
    _loaded = True


def get_adapter(adapter_id: str, config: dict[str, Any] | None = None) -> SourceAdapter:
    ensure_loaded()
    try:
        return _REGISTRY[adapter_id](config)
    except KeyError:
        raise KeyError(f"unknown adapter {adapter_id!r}") from None


def list_adapters() -> list[SourceAdapter]:
    """Default-configured instances of every registered adapter (sorted by default priority, id)."""
    ensure_loaded()
    inst = [cls() for cls in _REGISTRY.values()]
    return sorted(inst, key=lambda a: (a.info.default_priority, a.info.adapter_id))


_prio_cache: tuple[int, dict[str, int]] | None = None


def runnable_priority_map() -> dict[str, int]:
    """adapter_id -> default priority for non-info-only adapters (cached; used by persist for merge ranking)."""
    global _prio_cache
    ensure_loaded()
    if _prio_cache is None or _prio_cache[0] != len(_REGISTRY):
        _prio_cache = (len(_REGISTRY), {a: c.info.default_priority for a, c in _REGISTRY.items() if not c.info.info_only})
    return _prio_cache[1]


def adapter_infos() -> list[AdapterInfo]:
    return [a.info for a in list_adapters()]


# --------------------------------------------------------------------------- DB state
def _default_enabled(info: AdapterInfo) -> bool:
    return not (info.info_only or info.requires_config or not info.automated_collection_permitted)


async def sync_adapter_states(session: AsyncSession) -> int:
    """Create a SourceAdapterState row for every registered adapter (existing rows untouched)."""
    n = 0
    for info in adapter_infos():
        stmt = (
            pg_insert(SourceAdapterState)
            .values(adapter_id=info.adapter_id, enabled=_default_enabled(info), priority=info.default_priority, config={})
            .on_conflict_do_nothing(index_elements=["adapter_id"])
        )
        res = await session.execute(stmt)
        n += res.rowcount or 0
    await session.flush()
    return n


async def get_state(session: AsyncSession, adapter_id: str) -> SourceAdapterState | None:
    return await session.get(SourceAdapterState, adapter_id)


async def effective_priority(session: AsyncSession, adapter_id: str) -> int:
    """Priority from SourceAdapterState if a row exists, else the source-type default."""
    st = await get_state(session, adapter_id)
    if st is not None:
        return st.priority
    return get_adapter(adapter_id).info.default_priority


async def set_adapter_enabled(session: AsyncSession, adapter_id: str, enabled: bool) -> None:
    await sync_adapter_states(session)
    st = await session.get(SourceAdapterState, adapter_id)
    if st is None:
        raise KeyError(adapter_id)
    st.enabled = enabled
    await session.flush()


async def set_adapter_priority(session: AsyncSession, adapter_id: str, priority: int) -> None:
    await sync_adapter_states(session)
    st = await session.get(SourceAdapterState, adapter_id)
    if st is None:
        raise KeyError(adapter_id)
    st.priority = priority
    await session.flush()


async def set_adapter_config(session: AsyncSession, adapter_id: str, config: dict[str, Any]) -> None:
    await sync_adapter_states(session)
    st = await session.get(SourceAdapterState, adapter_id)
    if st is None:
        raise KeyError(adapter_id)
    st.config = dict(config)
    await session.flush()


async def record_adapter_run(
    session: AsyncSession, adapter_id: str, *, ok: bool, recovered: int = 0, error: str | None = None
) -> None:
    """Update last_run / success counters after a worker run of an adapter."""
    await sync_adapter_states(session)
    st = await session.get(SourceAdapterState, adapter_id)
    if st is None:
        raise KeyError(adapter_id)
    now = datetime.now(UTC)
    st.last_run_at = now
    if ok:
        st.last_success_at = now
        st.runs_ok = (st.runs_ok or 0) + 1
        st.last_error = None
    else:
        st.runs_failed = (st.runs_failed or 0) + 1
        st.last_error = (error or "")[:2000]
    st.businesses_recovered = (st.businesses_recovered or 0) + max(0, recovered)
    await session.flush()


async def ordered_adapters(
    session: AsyncSession, *, country_code: str | None = None, source_types: tuple[str, ...] | None = None,
    include_disabled: bool = False,
) -> list[tuple[SourceAdapter, int]]:
    """Runnable adapters ordered by effective priority: [(instance configured from DB state, priority)]."""
    await sync_adapter_states(session)
    rows = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    out: list[tuple[SourceAdapter, int]] = []
    for default in list_adapters():
        info = default.info
        st = rows.get(info.adapter_id)
        if info.info_only or not info.automated_collection_permitted:
            continue
        if st is not None and not st.enabled and not include_disabled:
            continue
        if country_code and not info.covers(country_code):
            continue
        if source_types and info.source_type not in source_types:
            continue
        prio = st.priority if st is not None else info.default_priority
        from .secret_config import open_config

        out.append((get_adapter(info.adapter_id, open_config(st.config) if st else None), prio))
    out.sort(key=lambda t: (t[1], t[0].info.adapter_id))
    return out


async def adapter_status(session: AsyncSession) -> list[dict[str, Any]]:
    """Info + DB state, for the API/UI."""
    await sync_adapter_states(session)
    rows = {r.adapter_id: r for r in (await session.execute(select(SourceAdapterState))).scalars()}
    out = []
    for a in list_adapters():
        st = rows.get(a.info.adapter_id)
        d = a.info.to_dict()
        d.update(
            enabled=st.enabled if st else False,
            priority=st.priority if st else a.info.default_priority,
            last_run_at=st.last_run_at if st else None,
            last_success_at=st.last_success_at if st else None,
            runs_ok=st.runs_ok if st else 0,
            runs_failed=st.runs_failed if st else 0,
            businesses_recovered=st.businesses_recovered if st else 0,
            last_error=st.last_error if st else None,
        )
        out.append(d)
    return out


async def count_states(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(SourceAdapterState))).scalar_one()
