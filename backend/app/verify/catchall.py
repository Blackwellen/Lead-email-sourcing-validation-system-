"""V06 -- catch-all (accept-all) evidence.

Evidence model: ``confirmed | suspected | rejected_unknown_recipient | unknown | untested``.

* A controlled random-nonexistent-recipient test runs **only** when :func:`is_probe_authorised` passes.
* For unauthorised domains no connection is made. Status is ``untested``, or ``suspected`` purely from
  passive evidence (a known accept-all *gateway* provider class, or imported delivery feedback showing
  accept-then-bounce behaviour).
* No outcome here ever means "verified deliverable". ``rejected_unknown_recipient`` only says the
  server refused a random address at SMTP time.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from .dnsmx import DomainMailInfo
from .smtp import ProbeResult, SmtpProbe, is_probe_authorised

CatchAllStatus = Literal["confirmed", "suspected", "rejected_unknown_recipient", "unknown", "untested"]

# Secure e-mail gateways commonly accept all recipients at the edge and filter/bounce later, so
# SMTP acceptance behind them is weak evidence. Passive hint only.
ACCEPT_ALL_GATEWAY_PROVIDERS = frozenset({"proofpoint", "mimecast", "barracuda", "sophos", "trendmicro", "spamexperts", "cisco_ironport", "mailanyone"})


@dataclass(slots=True)
class CatchAllResult:
    status: CatchAllStatus
    reason_code: str
    basis: str  # active_probe | provider_class | delivery_feedback | none
    probe: ProbeResult | None = None
    permission_context: str | None = None
    note: str = ""

    def detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"basis": self.basis, "note": self.note}
        if self.probe:
            d["probe"] = self.probe.detail()
        return d


def random_local_part() -> str:
    """A syntactically valid local part that cannot plausibly exist."""
    return f"zz-nx-{secrets.token_hex(7)}"


async def assess_catch_all(
    session: AsyncSession | None,
    domain: str,
    mail_info: DomainMailInfo | None,
    *,
    probe: SmtpProbe | None = None,
    delivery_feedback_accept_then_bounce: bool = False,
    run_active_test: bool = True,
) -> CatchAllResult:
    """Assess catch-all behaviour for ``domain``. Active testing needs explicit authorisation."""
    if mail_info is not None and mail_info.mail_status != "accepts_mail":
        return CatchAllResult("untested", "domain_not_accepting_mail", "none")
    auth = await is_probe_authorised(session, domain, settings=probe.settings if probe else None)
    if auth and run_active_test and probe is not None and mail_info is not None:
        hosts = [h.host for h in mail_info.mx_hosts_by_priority()]
        res = await probe.probe(session, f"{random_local_part()}@{domain}", hosts, domain=domain)
        ctx = res.permission_context or auth.context
        if res.recipient_evidence_status == "smtp_recipient_accepted":
            return CatchAllResult("confirmed", "random_recipient_accepted", "active_probe", res, ctx,
                                  "A random non-existent recipient was accepted: SMTP acceptance on this domain proves nothing about a mailbox.")
        if res.recipient_evidence_status == "smtp_recipient_rejected":
            return CatchAllResult("rejected_unknown_recipient", "random_recipient_rejected", "active_probe", res, ctx,
                                  "Server rejected a random recipient; it does not appear to accept all addresses (not a deliverability guarantee).")
        return CatchAllResult("unknown", f"probe_{res.reason_code}", "active_probe", res, ctx, "Probe was inconclusive.")
    if delivery_feedback_accept_then_bounce:
        return CatchAllResult("suspected", "accept_then_bounce_history", "delivery_feedback", note="Imported delivery feedback shows accept-then-bounce on this domain.")
    if mail_info is not None and mail_info.provider in ACCEPT_ALL_GATEWAY_PROVIDERS:
        return CatchAllResult("suspected", "accept_all_gateway_provider", "provider_class",
                              note=f"MX provider class '{mail_info.provider}' often accepts all recipients at the edge. Not tested (no authorisation).")
    return CatchAllResult("untested", "not_authorised_for_probe" if not auth else "active_test_not_requested", "none")
