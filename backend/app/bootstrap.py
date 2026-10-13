"""First-start bootstrap: create the initial administrator from the environment, only when no users exist."""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .models import User
from .security import audit, hash_password, validate_password_policy

log = logging.getLogger("bootstrap")


async def create_user(session: AsyncSession, email: str, password: str, role: str = "admin") -> User:
    from email_validator import EmailNotValidError, validate_email

    try:  # same validation as POST /api/users
        email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as e:
        raise ValueError(f"'{email[:80]}' is not a valid email address ({e})") from None
    problems = validate_password_policy(password, email)
    if problems:
        raise ValueError(" ".join(problems))
    u = User(email=email.strip().lower(), password_hash=hash_password(password), role=role)
    session.add(u)
    await session.flush()
    await audit(session, None, "user.bootstrap_create", "user", u.id, {"email": u.email, "role": role})
    return u


async def ensure_initial_admin(session: AsyncSession, settings: Settings) -> bool:
    """Create the admin from LEAD_INITIAL_ADMIN_EMAIL/PASSWORD iff the users table is empty. Never a default password."""
    if not (settings.initial_admin_email and settings.initial_admin_password):
        return False
    n = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if n:
        return False
    try:
        await create_user(session, settings.initial_admin_email, settings.initial_admin_password, "admin")
    except ValueError as e:
        log.error("initial administrator NOT created: %s", e)
        await session.rollback()
        return False
    await session.commit()
    log.warning("initial administrator created; remove LEAD_INITIAL_ADMIN_PASSWORD from the environment now")
    return True
