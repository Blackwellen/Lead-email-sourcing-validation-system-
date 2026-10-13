"""Smart source scheduler (Section B).

* ``compute_priority``  static source hierarchy x measured information gain per cost (Bayesian-smoothed) + a
  minimum-sample exploration bonus, damped by duplicate and failure rates. Higher = run sooner.
* ``SourceBudget`` / ``SourceBudgets``  HARD constraints: a sliding-window + token-bucket rate cap per adapter
  (``AdapterInfo.rate_policy``), concurrency cap and work quotas (per adapter, per category, overall). These are
  never traded off against priority.
* ``rebalance``  periodically updates ``priority_score`` and re-prioritises QUEUED jobs with a bounded change per
  step, enforces quotas (cancels queued work of exhausted sources) and applies a starvation guard.
* ``should_skip_expensive_work``  stops website/network checks being spent on duplicate / already-complete businesses.
* ``default_strategy_pipeline``  the ordered stage plan per strategy preset.
"""
from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Organisation,
    OrganisationDomain,
    RunOrganisation,
    SourceAdapterState,
    SourceEffectiveness,
    SourcingRun,
)
from .effectiveness import Metrics, metrics_by_adapter

# --------------------------------------------------------------------------- strategy pipeline
PIPELINE_STAGES = (
    "bulk_data", "company_matching", "official_domain_discovery", "own_website_crawl", "directory_corroboration",
    "document_extraction", "controlled_permutation_research", "verification", "qualification", "export",
)


@dataclass
class PipelineStage:
    name: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


_FAST_OFF = {"directory_corroboration", "document_extraction", "controlled_permutation_research"}
_BALANCED_OFF = {"controlled_permutation_research"}
STRATEGY_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {"off": _FAST_OFF, "crawl": {"max_pages": 4, "max_depth": 1, "time_budget_s": 25.0}, "domain_candidates": 3,
             "verify_mode": "fast"},
    "balanced": {"off": _BALANCED_OFF, "crawl": {"max_pages": 12, "max_depth": 2, "time_budget_s": 60.0}, "domain_candidates": 5,
                 "verify_mode": "fast"},
    "deep": {"off": set(), "crawl": {"max_pages": 30, "max_depth": 3, "time_budget_s": 120.0}, "domain_candidates": 8,
             "verify_mode": "deep"},
}


def default_strategy_pipeline(strategy: str) -> list[PipelineStage]:
    """Ordered stage plan: bulk data -> company matching -> official domain discovery -> own website crawl -> directory
    corroboration -> document extraction -> controlled permutation research -> verification -> qualification -> export.

    ``custom`` starts from ``balanced``; callers (``strategy.plan_from_config``) then apply the operator's choices.
    Controlled permutation research is only ever enabled by ``deep`` here and by an explicit
    ``contact_discovery='include_inferred'`` in the run config."""
    preset = STRATEGY_PRESETS.get(strategy if strategy != "custom" else "balanced")
    if preset is None:
        raise ValueError(f"unknown strategy {strategy!r}")
    params: dict[str, dict[str, Any]] = {
        "bulk_data": {}, "company_matching": {"auto_merge": "identifiers_and_exact_name_geo_only"},
        "official_domain_discovery": {"max_candidates": preset["domain_candidates"], "never_auto_confirm_guesses": True},
        "own_website_crawl": dict(preset["crawl"]),
        "directory_corroboration": {"info_only_sources_never_scraped": True},
        "document_extraction": {"max_documents_per_site": 2 if strategy != "deep" else 5},
        "controlled_permutation_research": {"max_role_candidates": 3, "origin": "role_speculative", "needs_confirmed_domain": True},
        "verification": {"mode": preset["verify_mode"]}, "qualification": {}, "export": {},
    }
    off = set(preset["off"])
    if strategy == "deep":
        off = {"controlled_permutation_research"}  # still opt-in via contact_discovery
    return [PipelineStage(n, n not in off, params[n]) for n in PIPELINE_STAGES]


# --------------------------------------------------------------------------- priority
@dataclass
class SchedulerConfig:
    w_static: float = 0.35
    w_gain: float = 0.45
    explore_bonus: float = 0.4
    min_sample: int = 20  # records seen before a source is no longer "new"
    prior_rate: float = 0.02  # prior expected contacts per cost unit
    prior_strength: float = 200.0  # cost units the prior is worth
    reference_rate: float = 0.05  # rate that maps to gain_score 0.5
    cpu_cost_per_second: float = 5.0  # cost units per CPU second (1 HTTP request = 1 unit)
    duplicate_penalty: float = 0.8
    failure_penalty: float = 1.0
    prio_min: int = 10
    prio_span: int = 100
    max_step: int = 10
    min_share: float = 0.1
    fairness_window: int = 50
    fairness_min_window: int = 10


@dataclass
class PriorityScore:
    score: float
    static: float
    gain: float
    exploration: float
    duplicate_multiplier: float
    failure_multiplier: float
    samples: int
    explanation: dict[str, Any] = field(default_factory=dict)


def static_score(default_priority: int) -> float:
    """Source hierarchy: bulk datasets (10) -> 1.0 ... manual imports (60) -> 0.5."""
    return round(max(0.0, min(1.0, 1.0 - (default_priority - 10) / 100.0)), 4)


def compute_priority(adapter_info: Any, effect: Metrics | dict | None, config: SchedulerConfig | None = None) -> PriorityScore:
    """Priority of a source from the static hierarchy and MEASURED effectiveness only.

    ``effect`` is a :class:`Metrics` (or a dict of raw counters). With no measurements the source sits on its static rank
    plus the full exploration bonus, so new sources get tried.
    """
    cfg = config or SchedulerConfig()
    c: dict[str, float] = {}
    derived: dict[str, Any] = {}
    if isinstance(effect, Metrics):
        c, derived = effect.counters, effect.derived
    elif isinstance(effect, dict):
        c = effect.get("counters", effect)
        derived = effect.get("derived", {})
    prio = getattr(adapter_info, "default_priority", None)
    if prio is None:
        prio = 50
    static = static_score(prio)
    samples = int(c.get("records_seen", 0) or 0)
    contacts = float(c.get("eligible_contacts", 0) or 0)
    cost = float(c.get("http_requests", 0) or 0) + cfg.cpu_cost_per_second * float(c.get("cpu_seconds", 0) or 0)
    # Bayesian (gamma-Poisson style) smoothing of contacts per cost unit towards the prior rate
    rate = (contacts + cfg.prior_rate * cfg.prior_strength) / (cost + cfg.prior_strength)
    gain = rate / (rate + cfg.reference_rate)
    explore = cfg.explore_bonus * max(0.0, 1.0 - samples / max(1, cfg.min_sample)) if cfg.min_sample else 0.0
    dup_pct = derived.get("duplicate_pct")
    if dup_pct is None and samples >= max(1, cfg.min_sample):
        dup_pct = 100.0 * float(c.get("duplicates", 0) or 0) / samples
    fail_pct = derived.get("failure_pct")
    jobs = float(c.get("jobs", 0) or 0)
    if fail_pct is None and jobs >= max(1, cfg.min_sample // 2):
        fail_pct = 100.0 * float(c.get("failures", 0) or 0) / jobs
    dup_mult = 1.0 - cfg.duplicate_penalty * min(1.0, (dup_pct or 0.0) / 100.0)
    fail_mult = 1.0 - min(0.9, cfg.failure_penalty * (fail_pct or 0.0) / 100.0)
    base = cfg.w_static * static + cfg.w_gain * gain + explore
    score = max(0.0, min(2.0, base * dup_mult * fail_mult))
    return PriorityScore(round(score, 5), static, round(gain, 5), round(explore, 5), round(dup_mult, 4), round(fail_mult, 4), samples,
                         {"contacts": contacts, "cost_units": round(cost, 3), "smoothed_rate": round(rate, 5),
                          "duplicate_pct": dup_pct, "failure_pct": fail_pct})


def job_priority_for(score: float, lo: float, hi: float, cfg: SchedulerConfig) -> int:
    """Map a score onto the job-priority axis (LOWER number = claimed first)."""
    norm = 0.5 if hi - lo < 1e-9 else (score - lo) / (hi - lo)
    return cfg.prio_min + int(round((1.0 - norm) * cfg.prio_span))


# --------------------------------------------------------------------------- hard constraints
class SourceBudget:
    """Token bucket + sliding window + concurrency + work quota for ONE adapter. Never exceeds any of them.

    ``requests_per_minute=None`` means no rate cap (local file); concurrency and quota still apply.
    The sliding 60 s window guarantees at most ``requests_per_minute`` grants in ANY 60 s span (a plain token bucket
    only guarantees rate + burst).
    """

    def __init__(self, adapter_id: str, requests_per_minute: int | None = None, concurrency: int = 1, max_work: int | None = None,
                 burst: int | None = None, clock: Callable[[], float] = time.monotonic):
        self.adapter_id = adapter_id
        self.rpm = requests_per_minute
        self.concurrency = max(1, concurrency)
        self.max_work = max_work
        self._clock = clock
        self.capacity = float(max(1, min(burst if burst is not None else 5, requests_per_minute or 5))) if requests_per_minute else 0.0
        self._tokens = self.capacity
        self._last = clock()
        self._window: deque[float] = deque()
        self.in_flight = 0
        self.work_used = 0
        self.denied = 0

    def _refill(self, now: float) -> None:
        if self.rpm:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rpm / 60.0)
        self._last = now
        if self.rpm:
            while self._window and now - self._window[0] >= 60.0:
                self._window.popleft()

    def can_acquire(self, n: int = 1) -> bool:
        now = self._clock()
        self._refill(now)
        if self.max_work is not None and self.work_used + n > self.max_work:
            return False
        if self.in_flight >= self.concurrency:
            return False
        if self.rpm:
            if self._tokens + 1e-9 < n or len(self._window) + n > self.rpm:
                return False
        return True

    def try_acquire(self, n: int = 1) -> bool:
        """Take ``n`` work units (requests). The caller MUST ``release()`` when the unit finishes."""
        if not self.can_acquire(n):
            self.denied += 1
            return False
        now = self._clock()
        if self.rpm:
            self._tokens -= n
            self._window.extend([now] * n)
        self.work_used += n
        self.in_flight += 1
        return True

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def time_until_available(self) -> float:
        """Seconds until a unit could be granted by the rate limits (0 = now; inf = quota exhausted)."""
        now = self._clock()
        self._refill(now)
        if self.max_work is not None and self.work_used >= self.max_work:
            return math.inf
        if not self.rpm:
            return 0.0
        wait = 0.0 if self._tokens >= 1 else (1 - self._tokens) * 60.0 / self.rpm
        if len(self._window) >= self.rpm:
            wait = max(wait, 60.0 - (now - self._window[0]))
        return max(0.0, wait)

    @property
    def exhausted(self) -> bool:
        return self.max_work is not None and self.work_used >= self.max_work


class SourceBudgets:
    """Registry combining adapter budgets with category and overall quotas (all hard)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, overall_max_work: int | None = None):
        self._clock = clock
        self.adapters: dict[str, SourceBudget] = {}
        self.category_of: dict[str, str | None] = {}
        self.category_quota: dict[str, int] = {}
        self.category_used: dict[str, int] = {}
        self.overall_max_work = overall_max_work
        self.overall_used = 0

    @classmethod
    def from_config(cls, infos: list[Any], discovery: Any, clock: Callable[[], float] = time.monotonic) -> SourceBudgets:
        from .strategy import category_of

        b = cls(clock, getattr(discovery, "overall_max_work", None))
        quotas = dict(getattr(discovery, "source_quotas", {}) or {})
        for i in infos:
            cat = category_of(i.adapter_id, i.source_type)
            b.category_of[i.adapter_id] = cat
            rp = i.rate_policy
            b.adapters[i.adapter_id] = SourceBudget(i.adapter_id, rp.requests_per_minute, rp.concurrency,
                                                    quotas.get(i.adapter_id), clock=clock)
            if cat and cat in quotas:
                b.category_quota[cat] = quotas[cat]
        return b

    def try_acquire(self, adapter_id: str, n: int = 1) -> bool:
        ab = self.adapters.get(adapter_id)
        if ab is None:
            return False
        cat = self.category_of.get(adapter_id)
        if self.overall_max_work is not None and self.overall_used + n > self.overall_max_work:
            return False
        if cat in self.category_quota and self.category_used.get(cat, 0) + n > self.category_quota[cat]:
            return False
        if not ab.try_acquire(n):
            return False
        self.overall_used += n
        if cat:
            self.category_used[cat] = self.category_used.get(cat, 0) + n
        return True

    def release(self, adapter_id: str) -> None:
        ab = self.adapters.get(adapter_id)
        if ab:
            ab.release()


# --------------------------------------------------------------------------- rebalance
@dataclass
class RebalanceResult:
    scores: dict[str, float] = field(default_factory=dict)
    priorities: dict[str, int] = field(default_factory=dict)  # mean queued priority after the step
    promoted: list[str] = field(default_factory=list)  # starvation guard
    cancelled: dict[str, int] = field(default_factory=dict)  # quota-exhausted adapters -> jobs cancelled
    changed_jobs: int = 0


_ADAPTER_EXPR = "COALESCE(payload->>'adapter_id', payload->>'source_adapter')"


def _info_for(adapter_id: str):
    from ..sources.base import SOURCE_TYPE_PRIORITY, get_adapter

    try:
        return get_adapter(adapter_id).info
    except Exception:
        class _Fallback:
            default_priority = SOURCE_TYPE_PRIORITY["website"]
            source_type = "website"
            rate_policy = None
        fb = _Fallback()
        fb.adapter_id = adapter_id
        return fb


async def rebalance(session: AsyncSession, run_id: int, *, config: SchedulerConfig | None = None) -> RebalanceResult:
    """One scheduler step for a run (call it every N seconds / N jobs from the supervisor)."""
    from ..run_config import DiscoverySpec
    from .strategy import category_of

    cfg = config or SchedulerConfig()
    out = RebalanceResult()
    run = await session.get(SourcingRun, run_id)
    spec = DiscoverySpec.model_validate((run.configuration_snapshot or {}).get("discovery") or {}) if run else DiscoverySpec()

    rows = (await session.execute(text(
        f"SELECT {_ADAPTER_EXPR} AS a, "
        "count(*) FILTER (WHERE state='queued') AS queued, "
        "count(*) FILTER (WHERE state IN ('leased','done','failed','dead')) AS work "
        "FROM sourcing_jobs WHERE run_id=:r GROUP BY 1"), {"r": run_id})).all()
    queued = {r.a: r.queued for r in rows if r.a and r.queued}
    work = {r.a: r.work for r in rows if r.a}
    if not queued:
        return out
    disabled = {s.adapter_id for s in (await session.execute(select(SourceAdapterState).where(SourceAdapterState.enabled.is_(False)))).scalars()}

    # ---- hard quotas first: exhausted sources lose their queued work
    infos = {a: _info_for(a) for a in queued}
    cat_work: dict[str, int] = {}
    for a, w in work.items():
        cat = category_of(a, getattr(_info_for(a), "source_type", None))
        if cat:
            cat_work[cat] = cat_work.get(cat, 0) + w
    total_work = sum(work.values())
    for a in list(queued):
        cat = category_of(a, getattr(infos[a], "source_type", None))
        reason = None
        if spec.overall_max_work is not None and total_work >= spec.overall_max_work:
            reason = "overall_work_limit"
        elif a in spec.source_quotas and work.get(a, 0) >= spec.source_quotas[a]:
            reason = "quota_exhausted"
        elif cat and cat in spec.source_quotas and cat_work.get(cat, 0) >= spec.source_quotas[cat]:
            reason = "quota_exhausted"
        if reason:
            r = await session.execute(text(
                f"UPDATE sourcing_jobs SET state='cancelled', finished_at=now(), error_code=:c WHERE run_id=:r AND state='queued' "
                f"AND {_ADAPTER_EXPR}=:a"), {"c": reason, "r": run_id, "a": a})
            out.cancelled[a] = r.rowcount or 0
            queued.pop(a)
    active = [a for a in queued if a not in disabled]
    if not active:
        return out

    # ---- scores
    glob = await metrics_by_adapter(session, None)
    runm = await metrics_by_adapter(session, run_id)
    scores: dict[str, float] = {}
    for a in active:
        eff = runm.get(a) if runm.get(a) and runm[a].counters.get("records_seen", 0) >= cfg.min_sample else glob.get(a)
        scores[a] = compute_priority(infos[a], eff, cfg).score
    out.scores = scores
    lo, hi = min(scores.values()), max(scores.values())
    for a, sc in scores.items():
        await session.execute(
            pg_insert(SourceEffectiveness).values(adapter_id=a, run_id=run_id, priority_score=sc)
            .on_conflict_do_update(constraint="uq_srceff", set_={"priority_score": sc}))

    # ---- bounded re-prioritisation of QUEUED jobs
    cur = {r.a: float(r.mean) for r in (await session.execute(text(
        f"SELECT {_ADAPTER_EXPR} AS a, avg(priority) AS mean FROM sourcing_jobs WHERE run_id=:r AND state='queued' GROUP BY 1"),
        {"r": run_id})).all() if r.a in active}
    for a in active:
        desired = job_priority_for(scores[a], lo, hi, cfg)
        delta = int(round(max(-cfg.max_step, min(cfg.max_step, desired - cur[a]))))
        if delta:
            r = await session.execute(text(
                f"UPDATE sourcing_jobs SET priority = GREATEST(1, LEAST(1000, priority + :d)) "
                f"WHERE run_id=:r AND state='queued' AND {_ADAPTER_EXPR}=:a"), {"d": delta, "r": run_id, "a": a})
            out.changed_jobs += r.rowcount or 0

    # ---- starvation guard: sources with queued work but too small a share of recent completions get promoted to the front
    recent = (await session.execute(text(
        f"SELECT {_ADAPTER_EXPR} AS a FROM sourcing_jobs WHERE run_id=:r AND state IN ('done','failed','dead') "
        "ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT :n"), {"r": run_id, "n": cfg.fairness_window})).all()
    if len(recent) >= cfg.fairness_min_window and len(active) > 1:
        shares: dict[str, int] = {}
        for (a,) in recent:
            shares[a] = shares.get(a, 0) + 1
        mins = {r.a: r.mn for r in (await session.execute(text(
            f"SELECT {_ADAPTER_EXPR} AS a, min(priority) AS mn FROM sourcing_jobs WHERE run_id=:r AND state='queued' GROUP BY 1"),
            {"r": run_id})).all() if r.a in active}
        for a in active:
            if shares.get(a, 0) / len(recent) < cfg.min_share:
                others = [v for k, v in mins.items() if k != a]
                if others and mins[a] >= min(others):
                    shift = mins[a] - min(others) + 1  # strictly ahead: ties would still be broken by job id
                    await session.execute(text(
                        f"UPDATE sourcing_jobs SET priority = GREATEST(1, priority - :s) WHERE run_id=:r AND state='queued' "
                        f"AND {_ADAPTER_EXPR}=:a"), {"s": shift, "r": run_id, "a": a})
                    out.promoted.append(a)
                    mins[a] -= shift
    out.priorities = {r.a: int(round(float(r.mean))) for r in (await session.execute(text(
        f"SELECT {_ADAPTER_EXPR} AS a, avg(priority) AS mean FROM sourcing_jobs WHERE run_id=:r AND state='queued' GROUP BY 1"),
        {"r": run_id})).all() if r.a in active}
    return out


# --------------------------------------------------------------------------- duplicate suppression
@dataclass
class SkipDecision:
    skip: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.skip


async def should_skip_expensive_work(
    session: AsyncSession, org_id: int, run_id: int | None = None, *, recent_days: int = 30, refresh: bool = False
) -> SkipDecision:
    """True when website/network work would be wasted on this business: it was merged into another record, was already
    processed in this run, or already has a confirmed domain, a recent crawl and fresh published contacts.
    ``refresh=True`` (business_scope refresh_existing) only keeps the merged / already-processed-in-run checks."""
    org = await session.get(Organisation, org_id)
    if org is None:
        return SkipDecision(True, "missing")
    if org.merged_into_id is not None:
        return SkipDecision(True, "merged_duplicate")
    if run_id is not None:
        stage = (await session.execute(select(RunOrganisation.stage).where(
            RunOrganisation.run_id == run_id, RunOrganisation.organisation_id == org_id))).scalar_one_or_none()
        if stage is not None and stage != "discovered":
            return SkipDecision(True, "already_processed_in_run")
    if refresh:
        return SkipDecision(False)
    cutoff = datetime.now(UTC) - timedelta(days=recent_days)
    dom = (await session.execute(select(OrganisationDomain).where(
        OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
        OrganisationDomain.parked.is_(False)).order_by(OrganisationDomain.last_crawled_at.desc().nulls_last()).limit(1))).scalar_one_or_none()
    if dom is None or dom.last_crawled_at is None or dom.last_crawled_at < cutoff:
        return SkipDecision(False)
    fresh = (await session.execute(text(
        "SELECT 1 FROM email_addresses WHERE organisation_id=:o AND origin IN ('published','corroborated') "
        "AND last_seen_at >= :c AND suppressed_at IS NULL LIMIT 1"), {"o": org_id, "c": cutoff})).first()
    if fresh:
        return SkipDecision(True, "confirmed_domain_and_fresh_contacts")
    return SkipDecision(True, "recently_crawled")
