"""Authentication, sessions, MFA and user management."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

import pyotp
from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import AuthSession, User
from ..security import (
    CSRF_COOKIE,
    LOCKOUT_MINUTES,
    MAX_FAILED_LOGINS,
    SESSION_COOKIE,
    ApiError,
    app_settings,
    audit,
    burn_password_check,
    client_ip,
    decrypt_secret,
    encrypt_secret,
    get_current_user,
    hash_password,
    lock_until,
    new_token,
    require_role,
    token_hash,
    utcnow,
    validate_password_policy,
    verify_password,
)
from .common import Ok, Page

log = logging.getLogger("auth")
router = APIRouter(prefix="/api", tags=["auth"])


# --------------------------------------------------------------------------- schemas
def normalise_email(v: str) -> str:
    try:
        return validate_email(v.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as e:
        raise ValueError(f"'{v[:80]}' is not a valid email address ({e})") from None


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)
    totp_code: str | None = Field(None, max_length=10)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool
    totp_enabled: bool
    created_at: datetime
    locked_until: datetime | None = None


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


class MfaSetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class MfaCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class MfaDisableIn(BaseModel):
    password: str = Field(max_length=256)
    code: str = Field(min_length=6, max_length=10)


class UserCreateIn(BaseModel):
    email: str
    password: str = Field(max_length=256)
    role: str = Field("operator", pattern="^(admin|operator|viewer)$")

    @field_validator("email")
    @classmethod
    def _e(cls, v: str) -> str:
        return normalise_email(v)


class UserUpdateIn(BaseModel):
    role: str | None = Field(None, pattern="^(admin|operator|viewer)$")
    is_active: bool | None = None
    unlock: bool | None = None
    email: str | None = None

    @field_validator("email")
    @classmethod
    def _e(cls, v: str | None) -> str | None:
        return normalise_email(v) if v else v


class PasswordResetIn(BaseModel):
    new_password: str | None = Field(None, max_length=256, description="Omit to generate a random one-time password")


class PasswordResetOut(BaseModel):
    user_id: int
    temporary_password: str | None = None


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, email=u.email, role=u.role, is_active=u.is_active, totp_enabled=u.totp_enabled, created_at=u.created_at, locked_until=u.locked_until)


def _set_cookies(response: Response, request: Request, token: str, csrf: str) -> None:
    st = app_settings(request)
    max_age = st.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax", secure=st.cookie_secure, path="/")
    # double-submit cookie: readable by the SPA so it can echo it in X-CSRF-Token
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, samesite="lax", secure=st.cookie_secure, path="/")


def _clear_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _check_policy(password: str, email: str | None) -> None:
    problems = validate_password_policy(password, email)
    if problems:
        raise ApiError(422, "weak_password", " ".join(problems), problems)


def verify_totp(user: User, code: str, request: Request | None = None) -> bool:
    if not user.totp_secret_enc:
        return False
    secret = decrypt_secret(user.totp_secret_enc, app_settings(request) if request else None)
    return bool(secret) and pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


# --------------------------------------------------------------------------- login / logout / me
@router.post("/auth/login", response_model=SessionOut)
async def login(body: LoginIn, request: Request, response: Response, session: AsyncSession = Depends(get_session)) -> SessionOut:
    ip = client_ip(request)
    limiter = request.app.state.login_limiter
    allowed, retry = limiter.check(ip)
    if not allowed:
        await audit(session, None, "auth.login_rate_limited", "ip", ip)
        await session.commit()
        raise ApiError(429, "rate_limited", "Too many sign-in attempts from this address. Try again in a minute.", headers={"Retry-After": str(int(retry) + 1)})
    generic = ApiError(401, "invalid_credentials", "Invalid email or password, or the account is temporarily locked.")
    email = body.email.strip().lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        burn_password_check(body.password)
        await audit(session, None, "auth.login_failed", "user", None, {"reason": "unknown_user", "ip": ip})
        await session.commit()
        raise generic
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        await audit(session, user, "auth.login_blocked_locked", "user", user.id, {"ip": ip})
        await session.commit()
        raise generic
    password_ok = verify_password(user.password_hash, body.password)
    mfa_ok = True
    if password_ok and user.totp_enabled:
        if not body.totp_code:
            # password was right but a code is needed; this is not counted as a failure
            raise ApiError(401, "mfa_required", "A verification code from your authenticator app is required.")
        mfa_ok = verify_totp(user, body.totp_code, request)
    if not (password_ok and mfa_ok and user.is_active):
        user.failed_logins = (user.failed_logins or 0) + 1
        locked = user.failed_logins >= MAX_FAILED_LOGINS
        if locked:
            user.locked_until = lock_until()
            user.failed_logins = 0
        await audit(session, user, "auth.login_failed", "user", user.id, {"ip": ip, "reason": "bad_code" if password_ok else "bad_password", "locked": locked})
        await session.commit()
        if locked:
            log.warning("account locked after repeated failures user_id=%s", user.id)
        raise generic
    user.failed_logins = 0
    user.locked_until = None
    await session.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at < now))
    token, csrf = new_token(), new_token()
    st = app_settings(request)
    expires = now + timedelta(hours=st.session_ttl_hours)
    session.add(AuthSession(id=token_hash(token), user_id=user.id, csrf_token=csrf, expires_at=expires, user_agent=(request.headers.get("user-agent") or "")[:300]))
    await audit(session, user, "auth.login", "user", user.id, {"ip": ip})
    await session.commit()
    _set_cookies(response, request, token, csrf)
    return SessionOut(user=user_out(user), csrf_token=csrf, expires_at=expires)


@router.post("/auth/logout", response_model=Ok)
async def logout(request: Request, response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> Ok:
    auth = request.state.auth_session
    await session.execute(delete(AuthSession).where(AuthSession.id == auth.id))
    await audit(session, user, "auth.logout", "user", user.id)
    await session.commit()
    _clear_cookies(response)
    return Ok(message="Signed out.")


@router.get("/me", response_model=SessionOut)
async def me(request: Request, user: User = Depends(get_current_user)) -> SessionOut:
    auth = request.state.auth_session
    return SessionOut(user=user_out(user), csrf_token=auth.csrf_token, expires_at=auth.expires_at)


@router.post("/auth/change-password", response_model=Ok)
async def change_password(body: PasswordChangeIn, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> Ok:
    if not verify_password(user.password_hash, body.current_password):
        await audit(session, user, "auth.change_password_failed", "user", user.id)
        await session.commit()
        raise ApiError(403, "wrong_password", "The current password is incorrect.")
    _check_policy(body.new_password, user.email)
    if body.new_password == body.current_password:
        raise ApiError(422, "weak_password", "The new password must differ from the current one.")
    u = await session.get(User, user.id)
    u.password_hash = hash_password(body.new_password)
    # sign out every other session
    await session.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.id != request.state.auth_session.id))
    await audit(session, user, "auth.change_password", "user", user.id)
    await session.commit()
    return Ok(message="Password changed. Other sessions were signed out.")


# --------------------------------------------------------------------------- MFA
@router.post("/auth/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> MfaSetupOut:
    if user.totp_enabled:
        raise ApiError(409, "mfa_already_enabled", "Two-factor authentication is already enabled. Disable it first to re-enrol.")
    secret = pyotp.random_base32()
    u = await session.get(User, user.id)
    u.totp_secret_enc = encrypt_secret(secret, app_settings(request))
    await audit(session, user, "auth.mfa_setup_started", "user", user.id)
    await session.commit()
    return MfaSetupOut(secret=secret, provisioning_uri=pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Lead Intelligence"))


@router.post("/auth/mfa/confirm", response_model=Ok)
async def mfa_confirm(body: MfaCodeIn, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> Ok:
    u = await session.get(User, user.id)
    if not u.totp_secret_enc or u.totp_enabled:
        raise ApiError(409, "mfa_not_pending", "No pending two-factor enrolment. Start with the setup step.")
    if not verify_totp(u, body.code, request):
        raise ApiError(422, "invalid_code", "That code is not valid. Check the time on your device and try again.")
    u.totp_enabled = True
    await audit(session, user, "auth.mfa_enabled", "user", user.id)
    await session.commit()
    return Ok(message="Two-factor authentication enabled.")


@router.post("/auth/mfa/disable", response_model=Ok)
async def mfa_disable(body: MfaDisableIn, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> Ok:
    u = await session.get(User, user.id)
    if not u.totp_enabled:
        raise ApiError(409, "mfa_not_enabled", "Two-factor authentication is not enabled.")
    if not verify_password(u.password_hash, body.password) or not verify_totp(u, body.code, request):
        await audit(session, user, "auth.mfa_disable_failed", "user", user.id)
        await session.commit()
        raise ApiError(403, "invalid_credentials", "Password or verification code is incorrect.")
    u.totp_enabled = False
    u.totp_secret_enc = None
    await audit(session, user, "auth.mfa_disabled", "user", user.id)
    await session.commit()
    return Ok(message="Two-factor authentication disabled.")


# --------------------------------------------------------------------------- user management (admin)
@router.get("/users", response_model=Page[UserOut], dependencies=[Depends(require_role("admin"))])
async def list_users(limit: int = Query(50, ge=1, le=200), cursor: str | None = None, session: AsyncSession = Depends(get_session)) -> Page[UserOut]:
    after = 0
    if cursor:
        try:
            after = int(cursor)
        except ValueError:
            raise ApiError(422, "invalid_cursor", "The pagination cursor is invalid.") from None
    rows = (await session.execute(select(User).where(User.id > after).order_by(User.id).limit(limit + 1))).scalars().all()
    nxt = str(rows[limit - 1].id) if len(rows) > limit else None
    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    return Page[UserOut](items=[user_out(u) for u in rows[:limit]], next_cursor=nxt, total_estimate=total)


async def _active_admin_count(session: AsyncSession, excluding: int | None = None) -> int:
    q = select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    if excluding is not None:
        q = q.where(User.id != excluding)
    return (await session.execute(q)).scalar_one()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreateIn, admin: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> UserOut:
    _check_policy(body.password, body.email)
    u = User(email=body.email, password_hash=hash_password(body.password), role=body.role)
    session.add(u)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "email_in_use", "A user with that email address already exists.") from None
    await audit(session, admin, "user.create", "user", u.id, {"email": u.email, "role": u.role})
    await session.commit()
    return user_out(u)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdateIn, admin: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> UserOut:
    u = await session.get(User, user_id)
    if u is None:
        raise ApiError(404, "not_found", "User not found.")
    changes: dict = {}
    losing_admin = (body.role is not None and body.role != "admin" and u.role == "admin") or (body.is_active is False and u.role == "admin" and u.is_active)
    if losing_admin:
        if u.id == admin.id:
            raise ApiError(409, "self_lockout", "You cannot demote or deactivate your own account.")
        if await _active_admin_count(session, excluding=u.id) == 0:
            raise ApiError(409, "last_admin", "At least one active administrator must remain.")
    if body.role is not None and body.role != u.role:
        changes["role"] = [u.role, body.role]
        u.role = body.role
    if body.email and body.email != u.email:
        changes["email_changed"] = True
        u.email = body.email
    if body.is_active is not None and body.is_active != u.is_active:
        changes["is_active"] = body.is_active
        u.is_active = body.is_active
        if not body.is_active:
            await session.execute(delete(AuthSession).where(AuthSession.user_id == u.id))
    if body.unlock:
        u.locked_until = None
        u.failed_logins = 0
        changes["unlocked"] = True
    if "role" in changes:  # privilege change invalidates existing sessions of that user
        await session.execute(delete(AuthSession).where(AuthSession.user_id == u.id))
    try:
        await audit(session, admin, "user.update", "user", u.id, changes)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "email_in_use", "A user with that email address already exists.") from None
    return user_out(u)


@router.post("/users/{user_id}/reset-password", response_model=PasswordResetOut)
async def reset_user_password(user_id: int, body: PasswordResetIn, admin: User = Depends(require_role("admin")), session: AsyncSession = Depends(get_session)) -> PasswordResetOut:
    u = await session.get(User, user_id)
    if u is None:
        raise ApiError(404, "not_found", "User not found.")
    generated = None
    pw = body.new_password
    if pw is None:
        pw = generated = secrets.token_urlsafe(18)
    else:
        _check_policy(pw, u.email)
    u.password_hash = hash_password(pw)
    u.failed_logins = 0
    u.locked_until = None
    await session.execute(update(AuthSession).where(AuthSession.user_id == u.id).values(expires_at=utcnow()))
    await audit(session, admin, "user.reset_password", "user", u.id)
    await session.commit()
    return PasswordResetOut(user_id=u.id, temporary_password=generated)


__all__ = ["router", "LOCKOUT_MINUTES"]
