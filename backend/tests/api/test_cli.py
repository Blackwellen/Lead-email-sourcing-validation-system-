import json
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
from sqlalchemy import select

from app.db import sessionmaker
from app.models import User

from .helpers import ApiClient, build_app, seed_org

ROOT = Path(__file__).resolve().parents[2]


def cli(*args: str, stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "app.cli", *args], input=stdin, capture_output=True, text=True, cwd=ROOT, timeout=120, env={**os.environ, **(env or {})})


async def test_create_admin_cli_enforces_password_policy_and_creates_loginable_admin():
    weak = cli("create-admin", "--email", "cli@example.com", "--password-stdin", stdin="short\n")
    assert weak.returncode == 2 and "at least 12" in weak.stderr
    assert "short" not in weak.stdout
    ok = cli("create-admin", "--email", "CLI@example.com", "--password-stdin", stdin="A-Long-CLI-Passphrase-42\n")
    assert ok.returncode == 0 and "Created admin cli@example.com" in ok.stdout and "Passphrase" not in ok.stdout + ok.stderr
    dup = cli("create-admin", "--email", "cli@example.com", "--password-stdin", stdin="A-Long-CLI-Passphrase-42\n")
    assert dup.returncode == 1
    async with sessionmaker()() as s:
        u = (await s.execute(select(User).where(User.email == "cli@example.com"))).scalar_one()
        assert u.role == "admin" and u.password_hash.startswith("$argon2id$")
    import httpx

    async with ApiClient(transport=httpx.ASGITransport(app=build_app()), base_url="http://test") as c:
        assert (await c.login("cli@example.com", "A-Long-CLI-Passphrase-42")).status_code == 200


async def test_create_admin_from_env_only_when_no_users():
    env = {"LEAD_INITIAL_ADMIN_EMAIL": "first@example.com", "LEAD_INITIAL_ADMIN_PASSWORD": "Initial-Admin-Passphrase-9"}
    first = cli("create-admin", "--from-env", env=env)
    assert first.returncode == 0
    second = cli("create-admin", "--from-env", env={**env, "LEAD_INITIAL_ADMIN_EMAIL": "second@example.com"})
    assert second.returncode == 1 and "users already exist" in second.stdout
    async with sessionmaker()() as s:
        assert [u.email for u in (await s.execute(select(User))).scalars()] == ["first@example.com"]
    nothing = cli("create-admin", "--from-env", env={"LEAD_INITIAL_ADMIN_EMAIL": "", "LEAD_INITIAL_ADMIN_PASSWORD": ""})
    assert nothing.returncode == 1  # never a default account


async def test_export_categories_and_bench(tmp_path):
    from sqlalchemy import text

    async with sessionmaker()() as s:
        await s.execute(text("INSERT INTO categories (slug,name,level) VALUES ('food','Food','category')"))
        await seed_org(s)
    out = tmp_path / "tax.json"
    r = cli("export-categories", "--format", "json", "--out", str(out))
    assert r.returncode == 0 and json.loads(out.read_text())["categories"][0]["slug"] == "food"
    r = cli("export-categories", "--format", "csv")
    assert r.stdout.splitlines()[0].startswith("slug,name,parent_slug") and "food,Food" in r.stdout
    assert "db-ping" in cli("bench", "list").stdout
    assert cli("bench", "run", "nope").returncode == 2
    assert cli("apply-retention").returncode == 0


async def test_migrate_creates_schema_from_scratch():
    admin = await asyncpg.connect("postgresql://lead:lead@localhost:5432/lead_test_api")
    try:
        await admin.execute("DROP DATABASE IF EXISTS lead_cli_migrate")
        await admin.execute("CREATE DATABASE lead_cli_migrate")
        url = "postgresql+asyncpg://lead:lead@localhost:5432/lead_cli_migrate"
        r = cli("migrate", env={"LEAD_DATABASE_URL": url})
        assert r.returncode == 0, r.stderr[-1500:]
        conn = await asyncpg.connect("postgresql://lead:lead@localhost:5432/lead_cli_migrate")
        try:
            tables = {x["tablename"] for x in await conn.fetch("select tablename from pg_tables where schemaname='public'")}
            ver = await conn.fetchval("select version_num from alembic_version")
        finally:
            await conn.close()
        assert {"users", "sessions", "email_addresses", "sourcing_jobs", "exports", "audit_events"} <= tables and ver
        # the API reports ready only when the DB is at the migration head
        import httpx

        from app.main import create_app

        app = create_app(build_app().state.settings.model_copy(update={"database_url": url, "env": "production"}))
        from app import db

        await db.dispose_engine()
        old = os.environ["LEAD_DATABASE_URL"]
        os.environ["LEAD_DATABASE_URL"] = url
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                j = (await c.get("/readyz")).json()
            assert j["ready"] is True and j["migration"]["current"] == j["migration"]["head"] == ver
        finally:
            os.environ["LEAD_DATABASE_URL"] = old
            get_settings.cache_clear()
            await db.dispose_engine()
    finally:
        await admin.execute("DROP DATABASE IF EXISTS lead_cli_migrate WITH (FORCE)")
        await admin.close()
