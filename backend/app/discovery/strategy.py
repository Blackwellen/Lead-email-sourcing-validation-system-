"""Translate ``RunConfig.discovery`` into concrete job-planning parameters (no I/O, deterministic)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..run_config import DiscoverySpec, RunConfig

# source category -> AdapterInfo.source_type values it covers (procurement / common crawl are matched by adapter id)
CATEGORY_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "bulk_open_data": ("bulk_dataset", "open_data"),
    "industry_registers": ("registry",),
    "licensed_directories": ("directory",),
    "company_websites": ("website",),
    "common_crawl": ("web_archive",),
}
_PROCUREMENT = re.compile(r"procure|tender|(^|_)ted($|_)|contracts?_?finder|find_a_tender|award", re.I)
_COMMON_CRAWL = re.compile(r"common_?crawl", re.I)
_EXHIBITOR = re.compile(r"exhibit|new_reg", re.I)


def category_of(adapter_id: str, source_type: str | None = None) -> str | None:
    """Operator-facing source category of an adapter (None when it fits none, e.g. manual imports)."""
    if _COMMON_CRAWL.search(adapter_id):
        return "common_crawl"
    if _PROCUREMENT.search(adapter_id):
        return "procurement"
    for cat, types in CATEGORY_SOURCE_TYPES.items():
        if source_type in types:
            return cat
    return None


def adapter_allowed(spec: DiscoverySpec, adapter_id: str, source_type: str | None = None) -> bool:
    """Is this adapter permitted by the selected source categories? Manual imports / unknown categories are always allowed
    (they are explicit operator choices); everything else must belong to a selected category."""
    cat = category_of(adapter_id, source_type)
    if cat is None:
        return True
    return cat in spec.source_categories


def contact_origins(spec: DiscoverySpec) -> tuple[str, ...]:
    """Email origins the run may produce/keep (see EmailAddress.origin)."""
    return {
        "published_only": ("published",),
        "published_plus_corroborated": ("published", "corroborated"),
        "include_inferred": ("published", "corroborated", "pattern_inferred", "role_speculative"),
    }[spec.contact_discovery]


@dataclass
class DiscoveryPlan:
    strategy: str
    stages: list[Any]  # scheduler.PipelineStage
    source_categories: list[str]
    contact_origins: tuple[str, ...]
    allow_inferred: bool
    include_new: bool
    refresh_existing: bool
    crawl: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    gap_recovery: dict[str, Any] = field(default_factory=dict)
    source_quotas: dict[str, int] = field(default_factory=dict)
    overall_max_work: int | None = None

    def stage(self, name: str):
        return next((s for s in self.stages if s.name == name), None)

    def stage_enabled(self, name: str) -> bool:
        s = self.stage(name)
        return bool(s and s.enabled)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["stages"] = [{"name": s.name, "enabled": s.enabled, "params": s.params} for s in self.stages]
        return d


def plan_from_config(cfg: RunConfig) -> DiscoveryPlan:
    """Concrete planning parameters for a run: stage plan, which source categories/contact origins are allowed, crawl,
    network, signal and gap-recovery settings. ``custom`` strategy starts from ``balanced`` and relies on the explicit
    operator choices (categories, network, signals ...)."""
    from .scheduler import default_strategy_pipeline

    d = cfg.discovery
    stages = default_strategy_pipeline(d.strategy)
    by = {s.name: s for s in stages}
    cats = set(d.source_categories)
    # explicit operator category choices switch stages on/off (never beyond what the strategy permits for 'fast')
    if d.strategy == "custom":
        by["own_website_crawl"].enabled = "company_websites" in cats
        by["directory_corroboration"].enabled = "licensed_directories" in cats
        by["document_extraction"].enabled = "documents" in cats
        by["controlled_permutation_research"].enabled = d.contact_discovery == "include_inferred"
    else:
        if "documents" not in cats:
            by["document_extraction"].enabled = False
        if "company_websites" not in cats:
            by["own_website_crawl"].enabled = False
        if "licensed_directories" not in cats:
            by["directory_corroboration"].enabled = False
    allow_inferred = d.contact_discovery == "include_inferred"
    by["controlled_permutation_research"].enabled = allow_inferred  # opt-in only, via contact_discovery
    crawl_stage = by["own_website_crawl"].params
    ladder = ["recheck_imported_website", "resolve_bulk_candidate_domains"]
    if d.search_budget > 0:
        ladder.append("search_provider")
    if "common_crawl" in cats:
        ladder.append("common_crawl_lookup")
    ladder.append("register_corroboration")
    if "documents" in cats:
        ladder.append("document_extraction")
    if allow_inferred:
        ladder.append("role_address_candidates")
    return DiscoveryPlan(
        strategy=d.strategy, stages=stages, source_categories=list(d.source_categories), contact_origins=contact_origins(d),
        allow_inferred=allow_inferred, include_new=d.business_scope in ("new_only", "both"),
        refresh_existing=d.business_scope in ("refresh_existing", "both") or cfg.refresh_existing,
        crawl={"max_pages": min(cfg.crawl_max_pages, crawl_stage.get("max_pages", cfg.crawl_max_pages)),
               "max_depth": crawl_stage.get("max_depth", 2), "time_budget_s": crawl_stage.get("time_budget_s", 60.0)},
        network={"enabled": bool(d.network.enabled),
                 "max_depth": d.network.max_depth, "source_budget": d.network.source_budget},
        signals={"enabled": d.signals.enabled, "types": list(d.signals.types), "freshness_days": d.signals.freshness_days},
        gap_recovery={"enabled": by["official_domain_discovery"].enabled, "policy": d.fallback_research,
                      "search_budget": d.search_budget, "ladder": ladder, "allow_inferred": allow_inferred},
        source_quotas=dict(d.source_quotas), overall_max_work=d.overall_max_work,
    )


def filter_adapters(spec: DiscoverySpec, infos: list[Any]) -> list[Any]:
    """Keep only AdapterInfo objects whose category the run selected."""
    return [i for i in infos if adapter_allowed(spec, i.adapter_id, i.source_type)]
