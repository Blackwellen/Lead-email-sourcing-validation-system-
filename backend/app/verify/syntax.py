"""V01 -- syntax validation and normalisation.

Uses ``email-validator`` with ``check_deliverability=False`` (no DNS here; DNS lives in
``dnsmx``). Syntax validity says nothing about whether a mailbox exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Literal

from email_validator import (
    EmailNotValidError,
    EmailSyntaxError,
    EmailUndeliverableError,
    validate_email,
)

SyntaxStatus = Literal["valid", "invalid", "unknown"]

MAX_TOTAL = 254
MAX_LOCAL = 64

# TLDs / suffixes reserved for special use (RFC 2606, 6761, 6762) -- never real mail destinations.
SPECIAL_USE_SUFFIXES = (
    "test", "invalid", "localhost", "example", "local", "onion", "internal", "lan", "home.arpa", "alt",
)
SPECIAL_USE_EXACT = ("example.com", "example.net", "example.org")


@dataclass(slots=True)
class SyntaxResult:
    """Outcome of V01."""

    status: SyntaxStatus
    normalised: str | None = None  # lowercased domain (IDNA/unicode form) + local part lowercased (for keys)
    local_part: str | None = None  # local part exactly as supplied (case preserved)
    domain: str | None = None  # unicode, lowercased domain
    ascii_domain: str | None = None  # IDNA/punycode domain
    error: str | None = None  # human-readable
    reason_code: str = "syntax_ok"
    smtputf8: bool = False  # local part needs SMTPUTF8


def _is_special_use(domain: str) -> bool:
    d = domain.lower().rstrip(".")
    if d in SPECIAL_USE_EXACT:
        return True
    return any(d == s or d.endswith("." + s) for s in SPECIAL_USE_SUFFIXES)


def strip_display_name(raw: str) -> str:
    """``"Jane Doe" <jane@x.com>`` -> ``jane@x.com``; strips ``mailto:`` too."""
    s = raw.strip()
    if s.lower().startswith("mailto:"):
        s = s[7:].split("?", 1)[0]
    if "<" in s and ">" in s:
        _, addr = parseaddr(s)
        if addr:
            return addr
    return s


def check_syntax(raw: str, *, strip_display: bool = False) -> SyntaxResult:
    """Validate and normalise ``raw``. Never raises; never does network I/O."""
    if raw is None or not isinstance(raw, str):
        return SyntaxResult("unknown", error="no address supplied", reason_code="syntax_empty")
    s = strip_display_name(raw) if strip_display else raw.strip()
    if not s:
        return SyntaxResult("invalid", error="address is empty", reason_code="syntax_empty")
    if len(s) > MAX_TOTAL + 2:
        return SyntaxResult("invalid", error=f"address longer than {MAX_TOTAL} characters", reason_code="syntax_too_long")
    if "@" in s and len(s.rsplit("@", 1)[0]) > MAX_LOCAL:
        return SyntaxResult("invalid", error=f"local part longer than {MAX_LOCAL} characters", reason_code="syntax_local_too_long")
    try:
        v = validate_email(s, check_deliverability=False, allow_smtputf8=True)
    except EmailUndeliverableError as e:  # pragma: no cover - only with deliverability checks
        return SyntaxResult("invalid", error=str(e), reason_code="syntax_undeliverable")
    except EmailSyntaxError as e:
        msg = str(e)
        low = msg.lower()
        code = "syntax_invalid"
        if "special-use" in low or "reserved" in low:
            code = "syntax_special_use_domain"
        elif "too long" in low:
            code = "syntax_too_long"
        elif "@" in low and "must have" in low:
            code = "syntax_missing_at"
        return SyntaxResult("invalid", error=msg, reason_code=code)
    except EmailNotValidError as e:
        return SyntaxResult("invalid", error=str(e), reason_code="syntax_invalid")
    except Exception as e:  # noqa: BLE001 - defensive: library bugs must not crash a batch
        return SyntaxResult("unknown", error=f"validator error: {e}", reason_code="syntax_validator_error")

    domain = (v.domain or "").lower()
    ascii_domain = (v.ascii_domain or domain).lower()
    local = v.local_part
    if _is_special_use(ascii_domain) or _is_special_use(domain):
        return SyntaxResult(
            "invalid", None, local, domain, ascii_domain,
            f"domain '{domain}' is reserved for special use and cannot receive internet mail",
            "syntax_special_use_domain",
        )
    if len(local.encode()) > MAX_LOCAL:
        return SyntaxResult("invalid", None, local, domain, ascii_domain, f"local part longer than {MAX_LOCAL} bytes", "syntax_local_too_long")
    normalised = f"{local.lower()}@{domain}"
    if len(normalised.encode()) > MAX_TOTAL + 20:
        return SyntaxResult("invalid", None, local, domain, ascii_domain, "address too long", "syntax_too_long")
    return SyntaxResult("valid", normalised, local, domain, ascii_domain, None, "syntax_ok", bool(getattr(v, "smtputf8", False)))
