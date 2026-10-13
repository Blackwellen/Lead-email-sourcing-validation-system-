"""Authentication, sessions, CSRF, RBAC, password policy, TOTP secret encryption, rate limiting and audit helper."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db import get_session
from .logging_setup import mask_pii
from .models import AuditEvent, AuthSession, User

log = logging.getLogger("security")

SESSION_COOKIE = "lead_session"
CSRF_COOKIE = "lead_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

# --------------------------------------------------------------------------- errors


class ApiError(Exception):
    """Raised anywhere in the API; rendered as ``{"error": {"code", "message", "detail"?}}``."""

    def __init__(self, status_code: int, code: str, message: str, detail: Any = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.headers = headers


# --------------------------------------------------------------------------- passwords
_ph = PasswordHasher()  # argon2id (library default), memory-hard parameters

COMMON_PASSWORDS = frozenset(
    """password password1 password12 password123 password1234 passw0rd p@ssw0rd p@ssword 123456 1234567 12345678 123456789
    1234567890 12345678910 123123123 111111111111 000000000000 qwerty qwertyuiop qwerty123 qwertyuiop123 qwerty123456 abc123
    abcdef123456 letmein letmein123 welcome welcome1 welcome123 admin admin123 administrator administrator1 iloveyou changeme
    changeme123 default monkey dragon football baseball master login princess sunshine trustno1 whatever zaq12wsx 1q2w3e4r
    1q2w3e4r5t 1qaz2wsx passwordpassword adminadmin123 admin1234567 letmein12345 welcome12345 correcthorsebatterystaple""".split()
)


def validate_password_policy(password: str, email: str | None = None) -> list[str]:
    """Return a list of human-readable problems (empty = acceptable)."""
    problems: list[str] = []
    if len(password) < 12:
        problems.append("Password must be at least 12 characters long.")
    if len(password) > 256:
        problems.append("Password must be at most 256 characters long.")
    low = password.lower()
    stripped = re.sub(r"[^a-z0-9@]", "", low)
    if low in COMMON_PASSWORDS or stripped in COMMON_PASSWORDS or re.sub(r"\d+$", "", stripped) in COMMON_PASSWORDS:
        problems.append("Password is too common; choose something less guessable.")
    if len(set(password)) < 5:
        problems.append("Password uses too few distinct characters.")
    if email:
        local = email.split("@")[0].lower()
        if len(local) >= 4 and local in low:
            problems.append("Password must not contain your email name.")
    return problems


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _ph.hash("dummy-password-for-timing-equalisation")


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification (used when the user does not exist)."""
    verify_password(_DUMMY_HASH, password)


# --------------------------------------------------------------------------- tokens
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet(settings: Settings | None = None) -> Fernet:
    key = hashlib.sha256(("totp-v1|" + (settings or get_settings()).secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str, settings: Settings | None = None) -> str:
    return _fernet(settings).encrypt(plain.encode()).decode()


def decrypt_secret(enc: str, settings: Settings | None = None) -> str | None:
    try:
        return _fernet(settings).decrypt(enc.encode()).decode()
    except InvalidToken:
        return None


# --------------------------------------------------------------------------- rate limiting
class SlidingWindowLimiter:
    """In-memory sliding-window limiter.

    NOTE: state lives in this process only. A deployment with several API instances (or workers
    behind a load balancer) needs a shared limiter (Redis, or enforcement at the reverse proxy);
    with N instances the effective limit is N x the configured value.
    """

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window = window_s
        self._hits: dict[str, deque[float]] = {}
        self._last_gc = time.monotonic()

    def check(self, key: str) -> tuple[bool, float]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.limit:
            return False, max(0.1, dq[0] + self.window - now)
        dq.append(now)
        if now - self._last_gc > self.window:
            self._gc(cutoff)
            self._last_gc = now
        return True, 0.0

    def peek_blocked(self, key: str) -> bool:
        dq = self._hits.get(key)
        if not dq:
            return False
        cutoff = time.monotonic() - self.window
        return sum(1 for t in dq if t > cutoff) >= self.limit

    def reset(self) -> None:
        self._hits.clear()

    def _gc(self, cutoff: float) -> None:
        for k in [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]:
            del self._hits[k]


# --------------------------------------------------------------------------- request -> user
def client_ip(request: Request) -> str:
    import os

    if os.environ.get("LEAD_TRUST_PROXY") == "1":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


async def resolve_session(session: AsyncSession, token: str | None) -> tuple[User, AuthSession] | None:
    if not token:
        return None
    row = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.id == token_hash(token), AuthSession.expires_at > datetime.now(UTC), User.is_active.is_(True))
        )
    ).first()
    if row is None:
        return None
    return row[1], row[0]


def check_csrf(request: Request, auth: AuthSession) -> None:
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    ok = (
        header
        and cookie
        and hmac.compare_digest(header.encode(), auth.csrf_token.encode())
        and hmac.compare_digest(cookie.encode(), auth.csrf_token.encode())
    )
    if not ok:
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token. Reload the page and try again.")


async def get_current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    found = await resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if found is None:
        raise ApiError(401, "not_authenticated", "You are not signed in, or your session has expired. Please sign in again.")
    user, auth = found
    if request.method not in SAFE_METHODS:
        check_csrf(request, auth)
        # Safety net: viewers are strictly read-only everywhere except managing their own account/session.
        if user.role == "viewer" and not request.url.path.startswith("/api/auth/"):
            raise ApiError(403, "forbidden", "Your role (viewer) is read-only and cannot make changes.")
    request.state.user = user
    request.state.auth_session = auth
    return user


def require_role(min_role: str):
    """Dependency factory: ``Depends(require_role('operator'))``. Admin > operator > viewer."""
    if min_role not in ROLE_RANK:
        raise ValueError(min_role)

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise ApiError(403, "forbidden", f"This action requires the '{min_role}' role; your role is '{user.role}'.")
        return user

    _dep._min_role = min_role  # type: ignore[attr-defined]  # introspected by the RBAC tests
    _dep.__name__ = f"require_{min_role}"
    return _dep


# --------------------------------------------------------------------------- audit
_SENSITIVE_KEYS = ("password", "token", "secret", "totp", "authorization", "cookie")


def _scrub(v: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {str(k): ("[redacted]" if any(x in str(k).lower() for x in _SENSITIVE_KEYS) else _scrub(x, depth + 1)) for k, x in v.items()}
    if isinstance(v, list | tuple | set):
        return [_scrub(x, depth + 1) for x in list(v)[:200]]
    if isinstance(v, str):
        return mask_pii(v)[:2000]
    if isinstance(v, datetime):
        return v.isoformat()
    if v is None or isinstance(v, bool | int | float):
        return v
    return mask_pii(str(v))[:500]


async def audit(
    session: AsyncSession,
    user: User | int | None,
    action: str,
    object_type: str | None = None,
    object_id: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an audit event (caller commits). Secrets are redacted and e-mail addresses masked."""
    uid = user.id if isinstance(user, User) else user
    session.add(
        AuditEvent(
            user_id=uid,
            action=action,
            object_type=object_type,
            object_id=None if object_id is None else str(object_id)[:64],
            detail=_scrub(detail or {}),
        )
    )
    await session.flush()


def utcnow() -> datetime:
    return datetime.now(UTC)


def lock_until() -> datetime:
    return utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
