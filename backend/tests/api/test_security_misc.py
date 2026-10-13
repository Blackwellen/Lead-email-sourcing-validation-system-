import logging

import httpx

from .helpers import PASSWORD, ApiClient, build_app, create_user, make_client


async def test_global_rate_limit_returns_429_with_retry_after():
    app = build_app(rate_limit_per_minute=5)
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        codes = [(await c.get("/api/me")).status_code for _ in range(8)]
        assert codes == [401] * 5 + [429] * 3
        r = await c.get("/api/me")
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1
        assert (await c.get("/healthz")).status_code == 200  # probes are exempt
        assert "x-content-type-options" in r.headers  # security headers also on rejected requests


async def test_secrets_and_tokens_never_reach_logs(caplog):
    caplog.set_level(logging.DEBUG)
    app = build_app()
    u = await create_user("alice.logger@example.com", "admin")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/auth/login", json={"email": u.email, "password": "Wrong-Password-That-Is-Long-1"})
        r = await c.login(u.email)
        token = c.cookies.get("lead_session")
        csrf = r.json()["csrf_token"]
        await c.get("/api/contacts", params={"q": "alice.logger@example.com"})
        await c.post("/api/users", json={"email": "someone.else@example.com", "password": "Super-Secret-Pass-77", "role": "viewer"})
        await c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "Brand-New-Passphrase-31"})
        await c.post("/api/auth/mfa/setup")
        await c.get("/api/audit")
        audit_text = (await c.get("/api/audit?limit=100")).text
        await c.post("/api/auth/logout")
    server = [rec for rec in caplog.records if rec.name != "httpx"]  # httpx is the test client's own logger, not the server's
    log_text = "\n".join(f"{rec.getMessage()} {rec.exc_text or ''}" for rec in server)
    assert server
    for secret in (PASSWORD, "Wrong-Password-That-Is-Long-1", "Super-Secret-Pass-77", "Brand-New-Passphrase-31", token, csrf):
        assert secret not in log_text, secret
        assert secret not in audit_text, secret
    assert "alice.logger@example.com" not in log_text and "someone.else@example.com" not in log_text  # no full e-mail addresses
    assert any("GET /api/contacts" in rec.getMessage() for rec in server)  # requests are still logged (path only)
    assert "q=alice" not in log_text


async def test_json_log_formatter_masks_emails():
    from app.logging_setup import JsonFormatter

    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "contact bob.smith@example.com failed", None, None)
    assert "bob.smith@example.com" not in JsonFormatter().format(rec)


async def test_audit_never_stores_secrets():
    from app.db import sessionmaker
    from app.security import audit

    async with sessionmaker()() as s:
        await audit(s, None, "test.event", "x", 1, {"password": "hunter2hunter2", "nested": {"api_token": "abc", "email": "jo@example.com"}, "csrf_token": "t"})
        await s.commit()
    async with make_client("admin") as c:
        item = (await c.get("/api/audit?action=test.event")).json()["items"][0]
    assert item["detail"] == {"password": "[redacted]", "nested": {"api_token": "[redacted]", "email": "j***@example.com"}, "csrf_token": "[redacted]"}


async def test_initial_admin_bootstrap_only_when_empty_and_never_default():
    from sqlalchemy import func, select

    from app.bootstrap import ensure_initial_admin
    from app.config import get_settings
    from app.db import sessionmaker
    from app.models import User

    base = get_settings()
    async with sessionmaker()() as s:
        assert await ensure_initial_admin(s, base.model_copy(update={"initial_admin_email": "", "initial_admin_password": ""})) is False  # no default admin
        assert await ensure_initial_admin(s, base.model_copy(update={"initial_admin_email": "root@example.com", "initial_admin_password": "password"})) is False  # weak -> refused
        assert (await s.execute(select(func.count()).select_from(User))).scalar_one() == 0
        assert await ensure_initial_admin(s, base.model_copy(update={"initial_admin_email": "Root@Example.com", "initial_admin_password": "A-Strong-Initial-Passphrase-1"})) is True
        assert await ensure_initial_admin(s, base.model_copy(update={"initial_admin_email": "other@example.com", "initial_admin_password": "A-Strong-Initial-Passphrase-2"})) is False
        u = (await s.execute(select(User))).scalar_one()
        assert u.email == "root@example.com" and u.role == "admin" and u.password_hash.startswith("$argon2id$")


async def test_production_refuses_default_secret_key():
    import pytest

    with pytest.raises(RuntimeError, match="LEAD_SECRET_KEY"):
        build_app(env="production", secret_key="change-me-in-production-please-use-32+chars")
    build_app(env="production", secret_key="x" * 40, cookie_secure=True)
