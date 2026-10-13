"""Explainable 0-100 confidence score and overall reachability label.

``reachability == "safe"`` means *no adverse evidence was found and the address was published or
corroborated on a domain that accepts mail*. It is **not** a guarantee of inbox delivery; nothing in
this system can promise that. Labels: ``safe | risky | invalid | unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Reachability = Literal["safe", "risky", "invalid", "unknown"]

SAFE_DISCLAIMER = (
    "'safe' means no adverse evidence was found for a published/corroborated address on a domain that accepts "
    "mail. It is not a guarantee of delivery or inbox placement."
)

SPECULATIVE_ORIGINS = frozenset({"pattern_inferred", "role_speculative"})
SPECULATIVE_CAP = 55
CATCHALL_CONFIRMED_CAP = 60
CATCHALL_SUSPECTED_CAP = 70
UNCERTAIN_DOMAIN_CAP = 60
DISPOSABLE_CAP = 20

SOURCE_POINTS = {"own_domain_page": 25, "manual": 18, "directory": 15, "dataset": 12, "third_party_page": 10, "inferred": 5}
MAX = {
    "syntax": 5, "source_quality": 25, "identity": 15, "corroboration": 15, "domain_mail": 15, "freshness": 10, "smtp": 15,
}


@dataclass(slots=True)
class ScoreInputs:
    origin: str = "published"  # published | corroborated | pattern_inferred | role_speculative
    source_kind: str | None = None  # own_domain_page | directory | dataset | manual | third_party_page | inferred
    source_reliability: float = 0.5
    domain_identity_confidence: float = 0.0  # 0..1 org <-> domain match
    contact_confidence: float = 0.0  # 0..1 person identity confidence
    corroborating_sources: int = 1
    syntax_status: str = "valid"
    domain_mail_status: str = "unknown"
    implicit_mx: bool = False
    recipient_evidence_status: str = "untested"
    catch_all_status: str = "untested"
    age_days: float | None = None  # since last observed on a source
    previously_bounced: bool = False
    hard_bounce: bool = False
    soft_bounces: int = 0
    complaint: bool = False
    conflicts: int = 0
    is_free_provider: bool = False
    is_disposable: bool = False
    role_type: str | None = None
    safe_threshold: int = 70
    disposable_reachability: Reachability = "risky"  # operator policy: 'risky' or 'invalid'
    pattern_confidence: float = 0.0  # 0..1 for pattern_inferred origins


@dataclass(slots=True)
class ScoreResult:
    score: int
    breakdown: list[dict[str, Any]]
    reachability: Reachability
    caps_applied: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    disclaimer: str = SAFE_DISCLAIMER

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "reachability": self.reachability, "components": self.breakdown,
                "caps_applied": self.caps_applied, "reasons": self.reasons, "disclaimer": self.disclaimer}


def _freshness(age_days: float | None) -> tuple[float, str]:
    if age_days is None:
        return 4.0, "observation date unknown"
    if age_days <= 90:
        return 10.0, f"observed {int(age_days)}d ago"
    if age_days >= 730:
        return 0.0, f"observed {int(age_days)}d ago (stale)"
    return round(10.0 * (730 - age_days) / (730 - 90), 1), f"observed {int(age_days)}d ago (decaying)"


def score_email(i: ScoreInputs) -> ScoreResult:
    """Compute an explainable score. Pure function: no I/O."""
    bd: list[dict[str, Any]] = []
    caps: list[str] = []
    reasons: list[str] = []

    def add(component: str, points: float, mx: float, note: str) -> float:
        bd.append({"component": component, "points": round(points, 1), "max": mx, "note": note})
        return points

    total = 0.0
    syntax_ok = i.syntax_status == "valid"
    total += add("syntax", MAX["syntax"] if syntax_ok else 0, MAX["syntax"], "syntax valid" if syntax_ok else f"syntax {i.syntax_status}")

    kind = i.source_kind or ("inferred" if i.origin in SPECULATIVE_ORIGINS else "directory")
    base = SOURCE_POINTS.get(kind, 8)
    if i.origin in SPECULATIVE_ORIGINS:
        base = min(base, 5) + round(5 * max(0.0, min(1.0, i.pattern_confidence)))
        note = f"speculative origin '{i.origin}': nothing publishes this address"
    else:
        base = base * (0.7 + 0.3 * max(0.0, min(1.0, i.source_reliability)))
        note = f"source kind '{kind}', reliability {i.source_reliability:.2f}"
    total += add("source_quality", base, MAX["source_quality"], note)

    ident = MAX["identity"] * max(0.0, min(1.0, i.domain_identity_confidence))
    total += add("identity", ident, MAX["identity"], f"organisation/domain identity confidence {i.domain_identity_confidence:.2f}")

    corr = 8 * max(0.0, min(1.0, i.contact_confidence)) + 3.5 * min(max(i.corroborating_sources - 1, 0), 2)
    if i.origin in SPECULATIVE_ORIGINS:
        corr = 0.0
    total += add("corroboration", corr, MAX["corroboration"], f"contact confidence {i.contact_confidence:.2f}, {i.corroborating_sources} source(s)")

    dm = {"accepts_mail": 15.0, "temporary_dns_failure": 4.0, "unknown": 3.0}.get(i.domain_mail_status, 0.0)
    if i.domain_mail_status == "accepts_mail" and i.implicit_mx:
        dm = 11.0
    total += add("domain_mail", dm, MAX["domain_mail"], f"domain mail status: {i.domain_mail_status}" + (" (implicit MX)" if i.implicit_mx else ""))

    fr, frn = _freshness(i.age_days)
    total += add("freshness", fr, MAX["freshness"], frn)

    smtp_pts = {"smtp_recipient_accepted": 15.0}.get(i.recipient_evidence_status, 0.0)
    smtp_note = f"SMTP evidence: {i.recipient_evidence_status} (acceptance is a diagnostic, not delivery)"
    if smtp_pts and i.catch_all_status == "confirmed":
        smtp_pts, smtp_note = 0.0, "SMTP acceptance ignored: domain accepts all recipients (catch-all)"
    elif smtp_pts and i.catch_all_status == "suspected":
        smtp_pts, smtp_note = 5.0, "SMTP acceptance discounted: catch-all suspected"
    elif smtp_pts and i.catch_all_status == "untested":
        smtp_pts, smtp_note = 10.0, "SMTP acceptance discounted: catch-all behaviour untested"
    total += add("smtp_evidence", smtp_pts, MAX["smtp"], smtp_note)

    # ---- penalties / adjustments
    if i.catch_all_status == "confirmed":
        total += add("catch_all_penalty", -15, 0, "domain accepts any recipient")
    elif i.catch_all_status == "suspected":
        total += add("catch_all_penalty", -6, 0, "catch-all suspected from passive evidence")
    if i.hard_bounce or i.previously_bounced:
        total += add("bounce_penalty", -40, 0, "previously hard-bounced")
    elif i.soft_bounces:
        total += add("bounce_penalty", -min(10, 3 * i.soft_bounces), 0, f"{i.soft_bounces} soft bounce(s)")
    if i.complaint:
        total += add("complaint_penalty", -25, 0, "recipient complained")
    if i.conflicts:
        total += add("conflict_penalty", -min(25, 10 * i.conflicts), 0, f"{i.conflicts} conflicting evidence item(s)")
    if i.is_free_provider:
        total += add("free_provider_adjustment", -10, 0, "free consumer mailbox provider (not tied to the organisation domain)")
    if i.role_type:
        total += add("role_adjustment", -5, 0, f"role account ({i.role_type}); shared inbox, not a named person")
    if i.is_disposable:
        total += add("disposable_penalty", -40, 0, "disposable email provider")

    score = int(round(max(0.0, min(100.0, total))))

    def cap(limit: int, why: str) -> None:
        nonlocal score
        if score > limit:
            score = limit
            caps.append(why)

    speculative = i.origin in SPECULATIVE_ORIGINS
    if speculative:
        cap(SPECULATIVE_CAP, f"speculative_origin_max_{SPECULATIVE_CAP}")
    if i.catch_all_status == "confirmed":
        cap(CATCHALL_CONFIRMED_CAP, f"catch_all_confirmed_max_{CATCHALL_CONFIRMED_CAP}")
    elif i.catch_all_status == "suspected":
        cap(CATCHALL_SUSPECTED_CAP, f"catch_all_suspected_max_{CATCHALL_SUSPECTED_CAP}")
    if i.domain_mail_status in ("unknown", "temporary_dns_failure"):
        cap(UNCERTAIN_DOMAIN_CAP, f"domain_mail_uncertain_max_{UNCERTAIN_DOMAIN_CAP}")
    if i.is_disposable:
        cap(DISPOSABLE_CAP, f"disposable_max_{DISPOSABLE_CAP}")

    # ---- reachability label
    reach: Reachability
    if not syntax_ok and i.syntax_status != "unknown":
        reach, why = "invalid", "syntax invalid"
        score = 0
        caps.append("syntax_invalid_zero")
    elif i.domain_mail_status in ("no_mail_service", "null_mx"):
        reach, why = "invalid", f"domain cannot receive mail ({i.domain_mail_status})"
        score = min(score, 5)
        caps.append("domain_no_mail_max_5")
    elif i.hard_bounce or i.previously_bounced:
        reach, why = "invalid", "previous hard bounce"
        score = min(score, 10)
        caps.append("hard_bounce_max_10")
    elif i.recipient_evidence_status == "smtp_recipient_rejected":
        reach, why = "invalid", "mail server rejected the recipient at SMTP time"
        score = min(score, 10)
        caps.append("smtp_rejected_max_10")
    elif i.is_disposable and i.disposable_reachability == "invalid":
        reach, why = "invalid", "disposable email domain"
    elif i.is_disposable:
        reach, why = "risky", "disposable email domain"
    elif i.syntax_status == "unknown" or i.domain_mail_status in ("unknown", "temporary_dns_failure"):
        reach, why = "unknown", "domain mail configuration could not be determined"
    elif speculative:
        reach = "risky" if i.recipient_evidence_status == "smtp_recipient_accepted" else "unknown"
        why = "speculative address (never published); cannot be 'safe'"
    elif i.catch_all_status in ("confirmed", "suspected"):
        reach, why = "risky", f"catch-all {i.catch_all_status}: acceptance proves nothing about this mailbox"
    elif i.complaint or i.soft_bounces or i.conflicts or i.is_free_provider or i.role_type:
        reach, why = "risky", "adverse or weak signals present (complaint, soft bounce, conflict, free provider or role account)"
    elif i.recipient_evidence_status == "inconclusive":
        reach, why = "risky", "SMTP diagnostics were inconclusive"
    elif score >= i.safe_threshold and i.domain_mail_status == "accepts_mail" and i.origin in ("published", "corroborated"):
        reach, why = "safe", "published/corroborated, domain accepts mail, no adverse evidence (not a delivery guarantee)"
    else:
        reach, why = "risky", f"score {score} below safe threshold {i.safe_threshold}"
    reasons.append(why)
    return ScoreResult(score, bd, reach, caps, reasons)
