import httpx
import pyotp
from sqlalchemy import select, text

from app.models import AuditEvent, AuthSession, User
from app.security import decrypt_secret

from .helpers import PASSWORD, ApiClient, build_app, create_user, make_admin_client


async def test_login_sets_secure_cookies_and_me(app, anon_client):
    u = await create_user("alice@example.com", "operator")
    r = await anon_client.post("/api/auth/login", json={"email": "Alice@Example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["email"] == "alice@example.com" and body["user"]["role"] == "operator" and body["csrf_token"]
    set_cookie = r.headers.get_list("set-cookie")
    sess = next(c for c in set_cookie if c.startswith("lead_session="))
    assert "HttpOnly" in sess and "SameSite=lax" in sess
    assert "Secure" not in sess  # cookie_secure is false in tests
    csrf = next(c for c in set_cookie if c.startswith("lead_csrf="))
    assert "HttpOnly" not in csrf
    me = await anon_client.get("/api/me")
    assert me.status_code == 200 and me.json()["user"]["id"] == u.id
    # only a hash of the token is stored
    token = anon_client.cookies.get("lead_session")
    from app.db import sessionmaker

    async with sessionmaker()() as s:
        ids = (await s.execute(select(AuthSession.id))).scalars().all()
    assert token not in ids and len(ids[0]) == 64


async def test_secure_flag_when_configured():
    app = build_app(cookie_secure=True)
    await create_user("sec@example.com", "viewer")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post("/api/auth/login", json={"email": "sec@example.com", "password": PASSWORD})
        assert "Secure" in next(x for x in r.headers.get_list("set-cookie") if x.startswith("lead_session="))
        assert r.headers["strict-transport-security"]


async def test_wrong_password_and_unknown_user_are_indistinguishable(anon_client):
    await create_user("bob@example.com", "viewer")
    a = await anon_client.post("/api/auth/login", json={"email": "bob@example.com", "password": "nope-nope-nope-1"})
    b = await anon_client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope-1"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()
    assert a.json()["error"]["code"] == "invalid_credentials"


async def test_lockout_after_repeated_failures():
    app = build_app()
    u = await create_user("lock@example.com", "operator")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        for _ in range(5):
            assert (await c.post("/api/auth/login", json={"email": u.email, "password": "wrong-wrong-wrong"})).status_code == 401
        # correct password is now rejected while locked
        r = await c.login(u.email)
        assert r.status_code == 401
        from app.db import sessionmaker

        async with sessionmaker()() as s:
            usr = (await s.execute(select(User).where(User.id == u.id))).scalar_one()
            assert usr.locked_until is not None
            await s.execute(text("UPDATE users SET locked_until = now() - interval '1 minute' WHERE id=:i"), {"i": u.id})
            await s.commit()
        assert (await c.login(u.email)).status_code == 200


async def test_login_rate_limit_per_ip():
    app = build_app(login_rate_limit_per_minute=3)
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        codes = [(await c.post("/api/auth/login", json={"email": f"x{i}@example.com", "password": "whatever-123456"})).status_code for i in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


async def test_csrf_required_on_unsafe_methods(app, operator_client):
    c = operator_client
    r = await c.post("/api/presets", json={"kind": "run", "name": "p1", "config": {}}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    c.cookies.delete("lead_csrf")
    r = await c.post("/api/presets", json={"kind": "run", "name": "p1", "config": {}})
    assert r.status_code == 403  # header alone is not enough: cookie missing (double submit)
    # GET never needs it
    assert (await c.get("/api/presets")).status_code == 200


async def test_unauthenticated_is_401(anon_client):
    for path in ("/api/me", "/api/runs", "/api/contacts", "/api/dashboard/overview", "/api/users"):
        r = await anon_client.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "not_authenticated"


async def test_logout_invalidates_session(operator_client):
    c = operator_client
    assert (await c.get("/api/me")).status_code == 200
    old = c.cookies.get("lead_session")
    r = await c.post("/api/auth/logout")
    assert r.status_code == 200
    c.cookies.set("lead_session", old)  # replay the old cookie
    assert (await c.get("/api/me")).status_code == 401


async def test_session_expiry(operator_client):
    from app.db import sessionmaker

    async with sessionmaker()() as s:
        await s.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
        await s.commit()
    assert (await operator_client.get("/api/me")).status_code == 401


async def test_deactivated_user_cannot_use_session(admin_client):
    v = await create_user("gone@example.com", "viewer")
    async with ApiClient(transport=httpx.ASGITransport(app=admin_client.app), base_url="http://test") as other:
        assert (await other.login(v.email)).status_code == 200
        r = await admin_client.patch(f"/api/users/{v.id}", json={"is_active": False})
        assert r.status_code == 200 and r.json()["is_active"] is False
        assert (await other.get("/api/me")).status_code == 401


async def test_mfa_enrolment_and_login(app):
    u = await create_user("mfa@example.com", "operator")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.login(u.email)).status_code == 200
        setup = (await c.post("/api/auth/mfa/setup")).json()
        assert setup["provisioning_uri"].startswith("otpauth://totp/") and setup["secret"]
        # stored encrypted, not plaintext
        from app.db import sessionmaker

        async with sessionmaker()() as s:
            enc = (await s.execute(select(User.totp_secret_enc).where(User.id == u.id))).scalar_one()
        assert enc != setup["secret"] and decrypt_secret(enc, app.state.settings) == setup["secret"]
        totp = pyotp.TOTP(setup["secret"])
        assert (await c.post("/api/auth/mfa/confirm", json={"code": "000000"})).status_code == 422
        assert (await c.post("/api/auth/mfa/confirm", json={"code": totp.now()})).status_code == 200
        await c.post("/api/auth/logout")
        # login now requires the code
        r = await c.login(u.email)
        assert r.status_code == 401 and r.json()["error"]["code"] == "mfa_required"
        assert (await c.login(u.email, totp_code="123456")).status_code == 401
        assert (await c.login(u.email, totp_code=totp.now())).status_code == 200
        # disable requires password and code
        assert (await c.post("/api/auth/mfa/disable", json={"password": "bad", "code": totp.now()})).status_code == 403
        assert (await c.post("/api/auth/mfa/disable", json={"password": PASSWORD, "code": totp.now()})).status_code == 200
        await c.post("/api/auth/logout")
        assert (await c.login(u.email)).status_code == 200


async def test_password_policy_and_change_password(operator_client):
    c = operator_client
    r = await c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "weak_password"
    r = await c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "password1234"})
    assert r.status_code == 422
    r = await c.post("/api/auth/change-password", json={"current_password": "wrong-wrong-wrong", "new_password": "A-very-long-new-passphrase-7"})
    assert r.status_code == 403
    r = await c.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "A-very-long-new-passphrase-7"})
    assert r.status_code == 200
    me = await c.get("/api/me")
    assert me.status_code == 200  # current session survives
    async with ApiClient(transport=httpx.ASGITransport(app=c.app), base_url="http://test") as fresh:
        assert (await fresh.login(c.email)).status_code == 401
        assert (await fresh.login(c.email, "A-very-long-new-passphrase-7")).status_code == 200


async def test_user_management(admin_client):
    c = admin_client
    r = await c.post("/api/users", json={"email": "new@example.com", "password": "weak", "role": "viewer"})
    assert r.status_code == 422
    r = await c.post("/api/users", json={"email": "new@example.com", "password": "Another-Strong-Pass-55", "role": "viewer"})
    assert r.status_code == 201
    uid = r.json()["id"]
    assert (await c.post("/api/users", json={"email": "NEW@example.com", "password": "Another-Strong-Pass-55"})).status_code == 409
    assert (await c.post("/api/users", json={"email": "not-an-email", "password": "Another-Strong-Pass-55"})).status_code == 422
    assert (await c.patch(f"/api/users/{uid}", json={"role": "operator"})).json()["role"] == "operator"
    lst = (await c.get("/api/users")).json()
    assert {u["email"] for u in lst["items"]} >= {"new@example.com"}
    rp = (await c.post(f"/api/users/{uid}/reset-password", json={})).json()
    assert rp["temporary_password"]
    async with ApiClient(transport=httpx.ASGITransport(app=c.app), base_url="http://test") as other:
        assert (await other.login("new@example.com", rp["temporary_password"])).status_code == 200
    # cannot demote/deactivate yourself or the last admin
    assert (await c.patch(f"/api/users/{c.user_id}", json={"role": "viewer"})).status_code == 409
    assert (await c.patch(f"/api/users/{c.user_id}", json={"is_active": False})).status_code == 409
    # audit trail
    acts = {a["action"] for a in (await c.get("/api/audit?limit=200")).json()["items"]}
    assert {"auth.login", "user.create", "user.update", "user.reset_password"} <= acts


async def test_failed_logins_and_logout_are_audited(app):
    u = await create_user("aud@example.com", "operator")
    async with ApiClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/auth/login", json={"email": u.email, "password": "wrong-wrong-wrong"})
        await c.login(u.email)
        await c.post("/api/auth/logout")
    from app.db import sessionmaker

    async with sessionmaker()() as s:
        acts = (await s.execute(select(AuditEvent.action))).scalars().all()
    assert {"auth.login_failed", "auth.login", "auth.logout"} <= set(acts)


async def test_make_admin_client_helper():
    async with make_admin_client() as c:
        assert (await c.get("/api/me")).json()["user"]["role"] == "admin"
