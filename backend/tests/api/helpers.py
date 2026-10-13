"""Reusable API test helpers: app factory, authenticated clients, data factories.

Other test modules can reuse the admin client::

    from tests.api.helpers import make_admin_client

    async def test_something():
        async with make_admin_client() as admin:
            r = await admin.post("/api/runs", json={...})   # CSRF header is attached automatically
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import sessionmaker
from app.main import create_app
from app.models import User
from app.security import hash_password

PASSWORD = "Tr0ub4dor&3-horse-staple"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def build_app(**overrides: Any) -> FastAPI:
    base: dict[str, Any] = {"rate_limit_per_minute": 1_000_000, "login_rate_limit_per_minute": 1_000, "secret_key": "test-secret-key-for-unit-tests-0123456789"}
    base.update(overrides)
    settings: Settings = get_settings().model_copy(update=base)
    return create_app(settings)


class ApiClient(httpx.AsyncClient):
    """httpx client that remembers the CSRF token returned by login and sends it on unsafe methods."""

    csrf: str | None = None
    user_id: int | None = None
    email: str | None = None

    async def request(self, method: str, url, **kw):  # type: ignore[override]
        if method.upper() in UNSAFE and self.csrf and "X-CSRF-Token" not in (kw.get("headers") or {}):
            kw["headers"] = {**(kw.get("headers") or {}), "X-CSRF-Token": self.csrf}
        return await super().request(method, url, **kw)

    async def login(self, email: str, password: str = PASSWORD, **extra: Any) -> httpx.Response:
        r = await self.post("/api/auth/login", json={"email": email, "password": password, **extra})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
            self.user_id = r.json()["user"]["id"]
            self.email = email
        return r


async def create_user(email: str | None = None, role: str = "operator", password: str = PASSWORD) -> User:
    email = email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    async with sessionmaker()() as s:
        u = User(email=email, password_hash=hash_password(password), role=role)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


@contextlib.asynccontextmanager
async def make_client(role: str = "operator", app: FastAPI | None = None, email: str | None = None) -> AsyncIterator[ApiClient]:
    app = app or build_app()
    u = await create_user(email, role)
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.login(u.email)
        assert r.status_code == 200, r.text
        c.app = app  # type: ignore[attr-defined]
        yield c


def make_admin_client(app: FastAPI | None = None, email: str | None = None):
    """Async context manager: creates an admin, logs in, attaches the CSRF token to unsafe requests."""
    return make_client("admin", app, email)


@pytest_asyncio.fixture
async def app():
    return build_app()


@pytest_asyncio.fixture
async def admin_client(app):
    async with make_client("admin", app) as c:
        yield c


@pytest_asyncio.fixture
async def operator_client(app):
    async with make_client("operator", app) as c:
        yield c


@pytest_asyncio.fixture
async def viewer_client(app):
    async with make_client("viewer", app) as c:
        yield c


@pytest_asyncio.fixture
async def anon_client(app):
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- data factories
async def seed_org(session, name="Acme Plumbing Ltd", country="GB", region="Greater London", city="London", category_id=None, source="companies_house_bulk", **kw) -> int:
    row = (await session.execute(text(
        """INSERT INTO organisations (legal_name, country_code, region, city, canonical_identity, discovery_source, category_id, legal_type, company_number)
           VALUES (:n, :c, :r, :ci, :ident, :src, :cat, :lt, :cn) RETURNING id"""),
        {"n": name, "c": country, "r": region, "ci": city, "ident": f"{name}|{uuid.uuid4().hex}", "src": source, "cat": category_id, "lt": kw.get("legal_type", "ltd"), "cn": kw.get("company_number")})).one()
    await session.commit()
    return row[0]


async def seed_email(session, org_id: int, email: str, *, origin="published", confidence=80.0, role_type=None, reachability="deliverable", marketing="eligible",
                     catch_all="not_catch_all", validated=True, contact_id=None, domain_status="accepts_mail", suppressed=False) -> int:
    local, domain = email.split("@")
    row = (await session.execute(text(
        """INSERT INTO email_addresses (organisation_id, contact_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, role_type,
              confidence, overall_reachability, marketing_status, catch_all_status, domain_mail_status, syntax_status, last_validated_at, suppressed_at)
           VALUES (:o, :cid, :e, :e, :l, :d, :d, 'website', :og, :rt, :cf, :re, :ms, :ca, :dm, 'valid', CASE WHEN :v THEN now() END, CASE WHEN :sup THEN now() END) RETURNING id"""),
        {"o": org_id, "cid": contact_id, "e": email.lower(), "l": local, "d": domain, "og": origin, "rt": role_type, "cf": confidence, "re": reachability, "ms": marketing,
         "ca": catch_all, "dm": domain_status, "v": validated, "sup": suppressed})).one()
    await session.commit()
    return row[0]


async def seed_contact(session, org_id: int, full_name="Jane Smith", title="Managing Director", key=None) -> int:
    row = (await session.execute(text(
        "INSERT INTO contacts (organisation_id, full_name, given_name, family_name, job_title, identity_key) VALUES (:o, :n, :g, :f, :t, :k) RETURNING id"),
        {"o": org_id, "n": full_name, "g": full_name.split()[0], "f": full_name.split()[-1], "t": title, "k": key or full_name.lower()})).one()
    await session.commit()
    return row[0]


async def seed_bulk_emails(session, n: int, org_id: int | None = None) -> int:
    """Insert n synthetic published e-mails quickly (one org per 5 emails) using generate_series."""
    if org_id is None:
        org_id = await seed_org(session, "Bulk Seed Org")
    await session.execute(text(
        """INSERT INTO organisations (legal_name, country_code, region, city, canonical_identity, discovery_source)
           SELECT 'Bulk Org ' || g, 'GB', 'Region ' || (g % 7), 'City ' || (g % 11), 'bulk|' || g, 'csv_import' FROM generate_series(1, :orgs) g"""), {"orgs": max(1, n // 5)})
    await session.execute(text(
        """INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type, origin, confidence,
              overall_reachability, marketing_status, syntax_status, domain_mail_status, last_validated_at)
           SELECT (SELECT min(id) FROM organisations WHERE canonical_identity LIKE 'bulk|%') + (g % GREATEST(1, :orgs)), 'user' || g || '@bulk' || (g % 997) || '.example.com', 'user' || g || '@bulk' || (g % 997) || '.example.com',
                  'user' || g, 'bulk' || (g % 997) || '.example.com', 'bulk' || (g % 997) || '.example.com', 'website', 'published', 50 + (g % 50), 'deliverable', 'eligible', 'valid', 'accepts_mail', now()
           FROM generate_series(1, :n) g"""), {"n": n, "orgs": max(1, n // 5)})
    await session.commit()
    await session.execute(text("ANALYZE email_addresses"))  # fresh bulk loads have no statistics yet (autovacuum would do this)
    await session.execute(text("ANALYZE organisations"))
    await session.commit()
    return org_id
