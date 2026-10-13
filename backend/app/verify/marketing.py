"""Marketing eligibility -- deliberately independent of technical verification.

A technically reachable address can still be unfit for marketing, and vice versa.

!!! This module is NOT legal advice. It applies an operator-configurable policy table
(``marketing_policy`` setting) and defaults to cautious values. Nothing is ever ``eligible`` unless the
operator's policy explicitly allows it (``allow_eligible: true``) and no suppression matches.
Operators remain responsible for compliance with PECR / GDPR / CAN-SPAM / CASL and local law.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MARKETING_STATUSES = ("eligible", "needs_review", "consent_required", "opted_out", "suppressed", "not_permitted")

EU_EEA_CH = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH".split()
)
UK_CODES = frozenset({"GB", "UK"})
NO_MARKETING_ROLES = frozenset({"noreply", "postmaster"})

# Keys per country: personal (named person at an incorporated body), role (role mailbox at a company),
# sole_trader (individual traders / unincorporated partnerships), free_provider (consumer mailbox),
# unknown_entity (cannot tell whether the entity is incorporated). `allow_eligible` gates the 'eligible' value.
DEFAULT_MARKETING_POLICY: dict[str, dict[str, Any]] = {
    "GB": {"personal": "needs_review", "role": "needs_review", "sole_trader": "consent_required", "free_provider": "consent_required", "unknown_entity": "needs_review", "allow_eligible": False},
    "EU": {"personal": "needs_review", "role": "needs_review", "sole_trader": "consent_required", "free_provider": "consent_required", "unknown_entity": "needs_review", "allow_eligible": False},
    "default": {"personal": "needs_review", "role": "needs_review", "sole_trader": "needs_review", "free_provider": "needs_review", "unknown_entity": "needs_review", "allow_eligible": False},
}
SOLE_TRADER_HINTS = ("sole", "individual", "unincorporated", "freelance", "self-employed", "self employed", "partnership", "proprietor")


@dataclass(frozen=True, slots=True)
class MarketingDecision:
    status: str
    reason: str
    note: str = "Policy-driven triage, not legal advice."


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def effective_policy(settings: Any, country: str | None) -> dict[str, Any]:
    """Resolve the policy row for ``country`` (exact -> UK/EU group -> default), merged over built-in defaults."""
    custom = _get(settings, "marketing_policy", None) or {}
    cc = (country or "").upper()
    group = "GB" if cc in UK_CODES else ("EU" if cc in EU_EEA_CH else "default")
    key = cc if cc in custom else group
    base = dict(DEFAULT_MARKETING_POLICY.get(key, DEFAULT_MARKETING_POLICY.get(group, DEFAULT_MARKETING_POLICY["default"])))
    row = custom.get(key, {}) or {}
    if hasattr(row, "model_dump"):
        row = row.model_dump()
    base.update(row)
    return base


def _is_sole_trader(legal_type: str | None) -> bool:
    lt = (legal_type or "").lower()
    if not lt:
        return False
    if "llp" in lt or "limited liability" in lt or "limited" in lt:
        return False
    return any(h in lt for h in SOLE_TRADER_HINTS)


def _matches_suppression(email: Any, org: Any, s: Any) -> bool:
    kind = (_get(s, "kind") or "").lower()
    value = (_get(s, "value") or "").strip().lower()
    if not value:
        return False
    norm = (_get(email, "email_normalised") or "").lower()
    domain = (_get(email, "domain") or "").lower()
    reg = (_get(email, "registrable_domain") or "").lower()
    if kind == "email":
        return value == norm
    if kind == "domain":
        return value in (domain, reg) or domain.endswith("." + value)
    if kind == "company":
        ids = {str(_get(org, "id", "")).lower(), (_get(org, "company_number") or "").lower(), (_get(org, "canonical_identity") or "").lower(),
               (_get(org, "legal_name") or "").lower(), (_get(org, "trading_name") or "").lower()}
        return value in ids
    return False


_MISSING = object()


def _eligibility_blocker(email_row: Any, live_evidence: bool | None) -> str | None:
    """Why an address may NOT be 'eligible' whatever the policy says (None = no blocker).

    Eligible requires a published (or corroborated) address with live evidence (not archive-only, not a permutation guess) that
    is not technically invalid, has not bounced before and is not a disposable mailbox. Fields the caller does not provide
    (plain objects in unit tests) are not checked; :func:`app.verify.pipeline.verify_email` always provides all of them."""
    origin = _get(email_row, "origin", _MISSING)
    if origin is not _MISSING and origin not in ("published", "corroborated"):
        return f"origin '{origin}' is not a published address"
    if live_evidence is False:
        return "no live evidence (archived or speculative evidence only)"
    if _get(email_row, "overall_reachability", None) == "invalid":
        return "address is technically invalid"
    if _get(email_row, "previously_bounced", False):
        return "address bounced before"
    if _get(email_row, "is_disposable", False):
        return "disposable mailbox"
    return None


def marketing_decision(email_row: Any, org: Any, suppressions: Iterable[Any], settings: Any = None, *,
                       live_evidence: bool | None = None) -> MarketingDecision:
    """Full decision with a human-readable reason. See :func:`marketing_status`.

    ``live_evidence`` (False = the address rests on archived / speculative evidence only) gates 'eligible' together with the
    address's origin, reachability, bounce and disposable flags."""
    hits = [s for s in suppressions if _matches_suppression(email_row, org, s)]
    if hits:
        if any((_get(s, "reason") in ("opt_out", "objection")) for s in hits):
            return MarketingDecision("opted_out", "recipient or organisation opted out / objected")
        return MarketingDecision("suppressed", f"suppression list match ({_get(hits[0], 'reason')})")
    role = _get(email_row, "role_type")
    if role in NO_MARKETING_ROLES:
        return MarketingDecision("not_permitted", f"'{role}' mailbox is not a marketing contact point")
    country = _get(org, "country_code")
    pol = effective_policy(settings, country)
    if _get(email_row, "is_free_provider"):
        outcome, why = pol.get("free_provider", "needs_review"), "free consumer mailbox provider (likely an individual)"
    elif _is_sole_trader(_get(org, "legal_type")):
        outcome, why = pol.get("sole_trader", "needs_review"), "organisation looks like a sole trader / unincorporated partnership (treated as an individual)"
    elif role:
        outcome, why = pol.get("role", "needs_review"), f"corporate role mailbox ({role})"
    elif not _get(org, "legal_type") and not _get(org, "company_number"):
        outcome, why = pol.get("unknown_entity", "needs_review"), "entity type unknown; personal-named address"
    else:
        outcome, why = pol.get("personal", "needs_review"), "named personal address at a corporate body"
    if outcome not in MARKETING_STATUSES or outcome in ("opted_out", "suppressed"):
        outcome = "needs_review"
    if outcome == "eligible" and not pol.get("allow_eligible"):
        return MarketingDecision("needs_review", why + "; 'eligible' requires allow_eligible in operator policy")
    if outcome == "eligible":
        blocker = _eligibility_blocker(email_row, live_evidence)
        if blocker:
            return MarketingDecision("needs_review", f"{why}; not eligible: {blocker}")
    return MarketingDecision(outcome, f"{why} [country policy: {country or 'default'}]")


def marketing_status(email_row: Any, org: Any, suppressions: Iterable[Any], settings: Any = None, *,
                     live_evidence: bool | None = None) -> str:
    """One of eligible|needs_review|consent_required|opted_out|suppressed|not_permitted (not legal advice)."""
    return marketing_decision(email_row, org, suppressions, settings, live_evidence=live_evidence).status
