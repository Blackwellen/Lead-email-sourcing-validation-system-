"""V07-V09 -- SMTP recipient-acceptance *diagnostics* (authorised domains only).

Hard guarantees (structural, not just policy):

* The only code path that writes to the socket is :meth:`SmtpProbe._send`, which asserts the verb is in
  ``ALLOWED_VERBS`` (EHLO, HELO, STARTTLS, MAIL, RCPT, RSET, QUIT, NOOP). There is no code that sends
  ``DATA`` (or ``BDAT``/``VRFY``/``EXPN``), so no message content can ever be transmitted.
* A probe is refused (``status="not_permitted"``, ``reason_code="not_authorised"``) unless
  :func:`is_probe_authorised` passes. In that case **no connection is opened**.
* No identity / IP rotation: one configured HELO name and one configured sender, from this host.
* A ``250`` at ``RCPT`` means *the server accepted the recipient at SMTP time*
  (``smtp_recipient_accepted``). It is never "deliverable" and says nothing about inbox placement.
"""
from __future__ import annotations

import asyncio
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..models import DomainPermission

ALLOWED_VERBS = frozenset({"EHLO", "HELO", "STARTTLS", "MAIL", "RCPT", "RSET", "QUIT", "NOOP"})

RecipientEvidence = Literal["smtp_recipient_accepted", "smtp_recipient_rejected", "inconclusive", "untested"]
ProbeStatus = Literal["completed", "inconclusive", "policy_restricted", "not_permitted", "not_configured"]


# --------------------------------------------------------------------------- authorisation
@dataclass(frozen=True, slots=True)
class ProbeAuthorisation:
    allowed: bool
    reason: str  # authorised | not_authorised | smtp_diagnostics_disabled
    context: str | None = None  # permission_context recorded on ValidationResult

    def __bool__(self) -> bool:
        return self.allowed


def _domain_chain(domain: str) -> list[str]:
    parts = domain.lower().strip().rstrip(".").split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] or [domain.lower()]


async def is_probe_authorised(session: AsyncSession | None, domain: str, *, settings: Settings | None = None) -> ProbeAuthorisation:
    """True only if diagnostics are enabled AND the domain (or a parent) is explicitly authorised.

    Authorisation sources: ``settings.smtp_authorised_domains`` or a ``DomainPermission`` row with
    ``smtp_probe_allowed``. Anything else -- including third-party domains -- is refused.
    """
    st = settings or get_settings()
    if not st.smtp_diagnostics_enabled:
        return ProbeAuthorisation(False, "smtp_diagnostics_disabled")
    chain = _domain_chain(domain)
    listed = {d.strip().lower().rstrip(".") for d in st.smtp_authorised_domains.split(",") if d.strip()}
    if any(c in listed for c in chain):
        return ProbeAuthorisation(True, "authorised", "settings_authorised_domains")
    if session is not None:
        rows = (await session.execute(
            select(DomainPermission.domain).where(DomainPermission.domain.in_(chain), DomainPermission.smtp_probe_allowed.is_(True))
        )).scalars().all()
        if rows:
            return ProbeAuthorisation(True, "authorised", "domain_permission_row")
    return ProbeAuthorisation(False, "not_authorised")


# --------------------------------------------------------------------------- reply classification
@dataclass(frozen=True, slots=True)
class ReplyClass:
    category: str  # accepted|rejected_user_unknown|mailbox_full|mailbox_disabled|deferred|policy_denied|unsupported|ok|other
    evidence: RecipientEvidence
    reason_code: str
    retryable: bool = False
    detail: str = ""


_ESC_RE = re.compile(r"^\s*([245])\.(\d{1,3})\.(\d{1,3})\b")
_USER_UNKNOWN = re.compile(
    r"user unknown|unknown user|no such (user|recipient|mailbox|address)|does ?n[o']t exist|not exist|"
    r"mailbox (unavailable|not found|does not exist)|no mailbox|recipient (not found|unknown|rejected)|"
    r"unknown recipient|invalid (recipient|mailbox|address)|address rejected|unrouteable address|"
    r"no account|account (not found|does not exist)|doesn'?t have an? .*account|bad recipient|undeliverable address",
    re.I)
_POLICY = re.compile(
    r"spamhaus|blacklist|blocklist|\bblocked\b|block list|\brbl\b|\bsbl\b|barracuda|reputation|\bptr\b|reverse dns|rdns|"
    r"relay(ing)? (not permitted|denied|access denied)|not authori[sz]ed|policy|spam|denied|banned|"
    r"poor reputation|rate limit|too many|abuse|dnsbl|sender (address )?rejected|access denied|forbidden|"
    r"authentication required|greylist|connection refused by|unsolicited|\bsorbs\b|\bcbl\b|ip address.*(listed|block)",
    re.I)
_FULL = re.compile(r"mailbox (is )?full|over ?quota|quota (exceeded|full)|storage (limit|full)|insufficient (storage|system storage)|exceeded storage", re.I)
_DISABLED = re.compile(r"disabled|suspended|deactivated|inactive|account (is )?closed|no longer (active|available)", re.I)
_GREY = re.compile(r"greylist|graylist|try again later|please retry|come back later|temporar", re.I)


def parse_enhanced(text: str) -> str | None:
    m = _ESC_RE.match(text or "")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else None


def classify_reply(code: int | None, enhanced: str | None, text: str, stage: str = "rcpt") -> ReplyClass:
    """Map one SMTP reply to an honest evidence class. Conservative: only clear signals are 'rejected'."""
    text = text or ""
    enh = enhanced or parse_enhanced(text)
    if code is None:
        return ReplyClass("other", "inconclusive", "smtp_no_reply", True)
    cls_digit = code // 100

    if stage != "rcpt":
        if cls_digit == 2 or (stage == "connect" and code == 220):
            return ReplyClass("ok", "untested", "smtp_stage_ok")
        if cls_digit == 4:
            return ReplyClass("deferred", "inconclusive", f"smtp_{stage}_deferred", True)
        if cls_digit == 5:
            if stage in ("ehlo",) and code in (500, 501, 502, 503, 504):
                return ReplyClass("unsupported", "inconclusive", "smtp_ehlo_unsupported")
            if code in (502, 503, 504, 500, 501) and stage == "starttls":
                return ReplyClass("unsupported", "inconclusive", "smtp_starttls_unsupported")
            return ReplyClass("policy_denied", "inconclusive", f"smtp_{stage}_refused", False, "refused before recipient stage")
        return ReplyClass("other", "inconclusive", f"smtp_{stage}_unexpected")

    # ---- RCPT stage
    if code in (250, 251):
        return ReplyClass("accepted", "smtp_recipient_accepted", "smtp_recipient_accepted", False,
                          "server accepted the recipient at SMTP time; this is not proof of delivery or an inbox")
    if (code == 452 and enh == "4.2.2") or (code == 552 and enh == "5.2.2") or (cls_digit in (4, 5) and _FULL.search(text) and enh not in ("5.1.1",)):
        return ReplyClass("mailbox_full", "inconclusive", "smtp_mailbox_full", cls_digit == 4)
    if cls_digit == 4:
        if code == 421:
            return ReplyClass("deferred", "inconclusive", "smtp_service_unavailable", True)
        if enh and enh.startswith("4.7"):
            return ReplyClass("deferred", "inconclusive", "smtp_temporary_policy", True)
        if _GREY.search(text) or code in (450, 451):
            return ReplyClass("deferred", "inconclusive", "smtp_greylisted_or_temporary", True)
        return ReplyClass("deferred", "inconclusive", "smtp_temporary_failure", True)
    if cls_digit == 5:
        if enh and enh.startswith("5.7"):
            return ReplyClass("policy_denied", "inconclusive", "smtp_policy_denied", False)
        if enh == "5.1.8":
            return ReplyClass("policy_denied", "inconclusive", "smtp_sender_policy", False)
        if enh == "5.2.1" or (_DISABLED.search(text) and not _USER_UNKNOWN.search(text) and enh != "5.1.1" and code in (550, 551, 553, 554)):
            if enh == "5.2.1" or re.search(r"mailbox|account|user|recipient", text, re.I):
                return ReplyClass("mailbox_disabled", "smtp_recipient_rejected", "smtp_mailbox_disabled", False)
        if enh and enh.startswith("5.1") and enh not in ("5.1.8",):
            return ReplyClass("rejected_user_unknown", "smtp_recipient_rejected", "smtp_recipient_rejected", False, enh)
        policy_hit = bool(_POLICY.search(text))
        if code in (554,) and policy_hit:
            return ReplyClass("policy_denied", "inconclusive", "smtp_policy_denied", False)
        if _USER_UNKNOWN.search(text) and not (policy_hit and enh is None and "spam" in text.lower()):
            return ReplyClass("rejected_user_unknown", "smtp_recipient_rejected", "smtp_recipient_rejected", False, "text hint")
        if policy_hit:
            return ReplyClass("policy_denied", "inconclusive", "smtp_policy_denied", False)
        if code in (500, 501, 502, 503, 504, 505):
            return ReplyClass("unsupported", "inconclusive", "smtp_command_unsupported", False)
        if code == 554:
            return ReplyClass("policy_denied", "inconclusive", "smtp_transaction_failed", False)
        return ReplyClass("other", "inconclusive", "smtp_permanent_unspecified", False,
                          "permanent failure without a clear unknown-user signal; not treated as invalid")
    return ReplyClass("other", "inconclusive", "smtp_unexpected_reply", False)


# --------------------------------------------------------------------------- result types
@dataclass(slots=True)
class SmtpReply:
    stage: str
    command: str  # sanitised verb (+ minimal argument shape)
    code: int | None
    enhanced: str | None
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "command": self.command, "code": self.code, "enhanced": self.enhanced, "text": self.text}


@dataclass(slots=True)
class ProbeResult:
    """Outcome of one SMTP diagnostic. ``recipient_evidence_status`` is evidence, not a deliverability claim."""

    status: ProbeStatus
    recipient_evidence_status: RecipientEvidence
    reason_code: str
    response_code: int | None = None
    enhanced_status_code: str | None = None
    reply_text: str | None = None
    reply_category: str | None = None
    mx_host: str | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    tls: dict[str, Any] = field(default_factory=dict)
    permission_context: str | None = None
    attempts: int = 0
    verbs_sent: list[str] = field(default_factory=list)
    duration_ms: int = 0
    address: str | None = None

    def detail(self) -> dict[str, Any]:
        return {
            "status": self.status, "mx_host": self.mx_host, "reply_category": self.reply_category, "reply_text": self.reply_text,
            "tls": self.tls, "attempts": self.attempts, "transcript": self.transcript, "duration_ms": self.duration_ms,
            "note": "SMTP acceptance is a diagnostic only; it is not proof of delivery or inbox placement.",
        }


class _ConnLost(Exception):
    pass


def _sanitise(text: str, limit: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[:limit].strip()


# --------------------------------------------------------------------------- probe
TargetResolver = Callable[[str], tuple[str, int]]


class SmtpProbe:
    """Authorised-domain SMTP recipient diagnostic. See module docstring for the guarantees."""

    def __init__(
        self,
        *,
        helo_host: str | None = None,
        mail_from: str | None = None,
        port: int = 25,
        connect_timeout: float = 10.0,
        command_timeout: float = 15.0,
        use_starttls: bool = True,
        max_deferral_retries: int = 2,
        backoff_base: float = 2.0,
        backoff_max: float = 10.0,
        settings: Settings | None = None,
        target_override: TargetResolver | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_mx_hosts: int = 3,
    ) -> None:
        st = settings or get_settings()
        self.settings = st
        self.mail_from = (mail_from if mail_from is not None else st.smtp_mail_from).strip()
        fallback_helo = self.mail_from.rsplit("@", 1)[-1] if "@" in self.mail_from else ""
        self.helo_host = (helo_host if helo_host is not None else st.smtp_helo_host) or fallback_helo
        self.port = port
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.use_starttls = use_starttls
        self.max_deferral_retries = max(0, max_deferral_retries)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.target_override = target_override
        self._sleep = sleep
        self.max_mx_hosts = max_mx_hosts
        self.verbs_sent: list[str] = []

    # -- the ONLY write path ------------------------------------------------------------------
    async def _send(self, w: asyncio.StreamWriter, line: str) -> None:
        verb = line.split(" ", 1)[0].split(":", 1)[0].upper().strip()
        assert verb in ALLOWED_VERBS, f"SMTP verb {verb!r} is forbidden (diagnostics never send message content)"
        assert "\r" not in line and "\n" not in line, "command injection guard"
        self.verbs_sent.append(verb)
        w.write(line.encode("utf-8") + b"\r\n")
        await asyncio.wait_for(w.drain(), self.command_timeout)

    async def _read_reply(self, r: asyncio.StreamReader, timeout: float) -> tuple[int | None, str | None, str]:
        lines: list[str] = []
        code: int | None = None
        for _ in range(100):
            try:
                raw = await asyncio.wait_for(r.readline(), timeout)
            except ValueError as e:  # over-long line
                raise _ConnLost("reply line too long") from e
            if not raw:
                raise _ConnLost("connection closed by server")
            s = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = re.match(r"^(\d{3})([ -]?)(.*)$", s)
            if not m:
                raise _ConnLost(f"malformed reply: {_sanitise(s, 60)}")
            code = int(m.group(1))
            lines.append(m.group(3))
            if m.group(2) != "-":
                break
        text = _sanitise(" | ".join(lines), 500)
        return code, parse_enhanced(lines[0] if lines else ""), text

    async def _cmd(self, r, w, stage: str, line: str, log: list[SmtpReply], shown: str | None = None) -> SmtpReply:
        await self._send(w, line)
        code, enh, text = await self._read_reply(r, self.command_timeout)
        rep = SmtpReply(stage, shown or line.split(" ", 1)[0], code, enh, text)
        log.append(rep)
        return rep

    def _targets(self, host: str) -> tuple[str, int]:
        return self.target_override(host) if self.target_override else (host, self.port)

    async def probe(
        self,
        session: AsyncSession | None,
        address: str,
        mx_hosts: Sequence[str],
        *,
        domain: str | None = None,
    ) -> ProbeResult:
        """Ask the domain's MX(es), in priority order, whether they would accept ``address`` as a recipient."""
        dom = (domain or address.rsplit("@", 1)[-1]).lower()
        auth = await is_probe_authorised(session, dom, settings=self.settings)
        if not auth:
            return ProbeResult("not_permitted", "untested", "not_authorised" if auth.reason != "smtp_diagnostics_disabled" else auth.reason,
                               address=address, reply_category="not_permitted",
                               transcript=[], permission_context=None)
        if not self.mail_from or "@" not in self.mail_from or not self.helo_host:
            return ProbeResult("not_configured", "untested", "smtp_sender_not_configured", address=address, permission_context=auth.context)
        if not mx_hosts:
            return ProbeResult("inconclusive", "inconclusive", "smtp_no_mx_hosts", address=address, permission_context=auth.context)

        t0 = time.monotonic()
        self.verbs_sent = []
        transcript: list[dict[str, Any]] = []
        tls: dict[str, Any] = {}
        last: ProbeResult | None = None
        attempts = 0
        for host in list(mx_hosts)[: self.max_mx_hosts]:
            for retry in range(self.max_deferral_retries + 1):
                attempts += 1
                res = await self._attempt(host, address, transcript, tls)
                last = res
                if res.reply_category == "deferred" and retry < self.max_deferral_retries:
                    await self._sleep(min(self.backoff_max, self.backoff_base * (2 ** retry)))
                    continue
                break
            # fall back to the next MX only on connection-level trouble, never after a real SMTP answer
            if last.reply_category != "connect_error":
                break
        assert last is not None
        last.transcript = transcript
        last.tls = tls
        last.attempts = attempts
        last.permission_context = auth.context
        last.verbs_sent = list(self.verbs_sent)
        last.duration_ms = int((time.monotonic() - t0) * 1000)
        last.address = address
        assert set(self.verbs_sent) <= ALLOWED_VERBS
        return last

    async def _attempt(self, host: str, address: str, transcript: list[dict[str, Any]], tls: dict[str, Any]) -> ProbeResult:
        addr, port = self._targets(host)
        allow_tls = self.use_starttls
        for _round in range(2):  # second round only if STARTTLS handshake broke the connection
            log: list[SmtpReply] = []
            reader = writer = None
            try:
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(addr, port), self.connect_timeout)
                except TimeoutError:
                    return ProbeResult("inconclusive", "inconclusive", "smtp_connect_timeout", mx_host=host, reply_category="connect_error")
                except OSError as e:
                    return ProbeResult("inconclusive", "inconclusive", "smtp_connect_failed", mx_host=host, reply_category="connect_error",
                                       reply_text=_sanitise(str(e), 120))
                code, enh, text = await self._read_reply(reader, self.connect_timeout)
                log.append(SmtpReply("connect", "(greeting)", code, enh, text))
                if code != 220:
                    rc = classify_reply(code, enh, text, "connect")
                    return self._result(host, rc, code, enh, text, policy=True)
                rep = await self._cmd(reader, writer, "ehlo", f"EHLO {self.helo_host}", log)
                extensions = rep.text.upper()
                if rep.code != 250:
                    rep = await self._cmd(reader, writer, "ehlo", f"HELO {self.helo_host}", log)
                    extensions = ""
                    if rep.code != 250:
                        return self._result(host, classify_reply(rep.code, rep.enhanced, rep.text, "ehlo"), rep.code, rep.enhanced, rep.text, policy=True)
                if allow_tls and "STARTTLS" in extensions:
                    rep = await self._cmd(reader, writer, "starttls", "STARTTLS", log)
                    if rep.code == 220:
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE  # diagnostics record TLS capability; no trust decision is made
                        try:
                            await asyncio.wait_for(writer.start_tls(ctx, server_hostname=host), self.connect_timeout)
                        except (ssl.SSLError, OSError, TimeoutError, ConnectionError) as e:
                            tls.update({"started": False, "tls_failed": True, "error": _sanitise(f"{type(e).__name__}: {e}", 120)})
                            allow_tls = False  # reconnect once without STARTTLS (same host/identity)
                            await self._close(writer, quit_=False)
                            writer = None
                            continue
                        so = writer.get_extra_info("ssl_object")
                        tls.update({"started": True, "tls_failed": False, "tls_version": so.version() if so else None,
                                    "cipher": so.cipher()[0] if so and so.cipher() else None, "verified": False})
                        rep = await self._cmd(reader, writer, "ehlo", f"EHLO {self.helo_host}", log)
                        if rep.code != 250:
                            return self._result(host, classify_reply(rep.code, rep.enhanced, rep.text, "ehlo"), rep.code, rep.enhanced, rep.text, policy=True)
                    else:
                        tls.update({"started": False, "tls_failed": True, "error": f"STARTTLS refused: {rep.code}"})
                elif "STARTTLS" not in extensions:
                    tls.setdefault("started", False)
                    tls.setdefault("offered", False)
                rep = await self._cmd(reader, writer, "mail", f"MAIL FROM:<{self.mail_from}>", log)
                if rep.code is None or rep.code // 100 != 2:
                    res = self._result(host, classify_reply(rep.code, rep.enhanced, rep.text, "mail"), rep.code, rep.enhanced, rep.text, policy=True)
                    await self._close(writer)
                    return res
                rep = await self._cmd(reader, writer, "rcpt", f"RCPT TO:<{address}>", log, shown="RCPT TO:<recipient>")
                rc = classify_reply(rep.code, rep.enhanced, rep.text, "rcpt")
                res = self._result(host, rc, rep.code, rep.enhanced, rep.text)
                try:
                    await self._cmd(reader, writer, "rset", "RSET", log)
                except (_ConnLost, TimeoutError, OSError):
                    pass
                await self._close(writer)
                writer = None
                return res
            except _ConnLost as e:
                return ProbeResult("inconclusive", "inconclusive", "smtp_connection_lost", mx_host=host, reply_category="connect_error",
                                   reply_text=_sanitise(str(e), 120))
            except TimeoutError:
                return ProbeResult("inconclusive", "inconclusive", "smtp_command_timeout", mx_host=host, reply_category="connect_error")
            except (OSError, ssl.SSLError) as e:
                return ProbeResult("inconclusive", "inconclusive", "smtp_connection_error", mx_host=host, reply_category="connect_error",
                                   reply_text=_sanitise(f"{type(e).__name__}: {e}", 120))
            finally:
                transcript.extend(x.as_dict() for x in log)
                if writer is not None:
                    await self._close(writer, quit_=False)
        return ProbeResult("inconclusive", "inconclusive", "smtp_tls_failed", mx_host=host, reply_category="connect_error")

    def _result(self, host: str, rc: ReplyClass, code, enh, text, *, policy: bool = False) -> ProbeResult:
        if rc.category == "policy_denied":
            status: ProbeStatus = "policy_restricted"
        elif rc.evidence == "inconclusive":
            status = "inconclusive"
        else:
            status = "completed"
        if policy and rc.category not in ("policy_denied", "deferred"):
            status = "inconclusive"
        return ProbeResult(status, rc.evidence, rc.reason_code, code, enh or parse_enhanced(text or ""), text, rc.category, host)

    async def _close(self, w: asyncio.StreamWriter, quit_: bool = True) -> None:
        try:
            if quit_:
                await self._send(w, "QUIT")
                await asyncio.sleep(0)
        except Exception:  # noqa: BLE001
            pass
        try:
            w.close()
            await asyncio.wait_for(w.wait_closed(), 2)
        except Exception:  # noqa: BLE001
            pass
