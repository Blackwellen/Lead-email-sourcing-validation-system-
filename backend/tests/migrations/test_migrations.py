"""The schema built ONLY by alembic must equal the models and work with raw SQL (server defaults!)."""
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import jobqueue as jq
from app.config import get_settings

pytestmark = pytest.mark.asyncio


def _alembic(url: str, *args: str):
    env = {**os.environ, "LEAD_DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True)


async def test_migrations_build_a_working_schema():
    base = get_settings().database_url
    admin = create_async_engine(base.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    name = "lead_mig_" + uuid.uuid4().hex[:8]
    url = base.rsplit("/", 1)[0] + "/" + name
    async with admin.connect() as c:
        await c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        r = _alembic(url, "upgrade", "head")
        assert r.returncode == 0, r.stderr[-800:]
        chk = _alembic(url, "check")
        assert chk.returncode == 0 and "No new upgrade operations" in (chk.stdout + chk.stderr), chk.stdout + chk.stderr[-800:]
        eng = create_async_engine(url)
        async with eng.connect() as conn:
            # raw SQL relying on server defaults only
            rid = (await conn.execute(text(
                "INSERT INTO sourcing_runs (name, configuration_snapshot, target_count) VALUES ('m', '{}'::jsonb, 1) RETURNING id"))).scalar_one()
            assert (await conn.execute(text("SELECT status FROM sourcing_runs WHERE id=:i"), {"i": rid})).scalar_one() == "draft"
            await conn.execute(text("UPDATE sourcing_runs SET status='running' WHERE id=:i"), {"i": rid})
            await conn.commit()
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with async_sessionmaker(eng, expire_on_commit=False)() as s:
            assert await jq.enqueue(s, run_id=rid, stage="verify", dedupe_key="k", payload={"email_id": 1})
            await s.commit()
            jobs = await jq.claim(s, "w1", ["verify"], 5, 30)
            assert len(jobs) == 1 and jobs[0].attempts == 1
            await s.execute(text("INSERT INTO organisations (country_code, canonical_identity) VALUES ('GB','x')"))
            await s.execute(text("INSERT INTO email_addresses (organisation_id, email_original, email_normalised, local_part, domain, registrable_domain, source_type) "
                                 "SELECT id,'a@x.co','a@x.co','a','x.co','x.co','mailto' FROM organisations"))
            await s.execute(text("INSERT INTO evidence_sources (source_adapter, evidence_type) VALUES ('t','mailto')"))
            assert (await s.execute(text("SELECT historical FROM evidence_sources"))).scalar_one() is False
            await s.commit()
        await eng.dispose()
        assert _alembic(url, "downgrade", "base").returncode == 0
        assert _alembic(url, "upgrade", "head").returncode == 0
    finally:
        async with admin.connect() as c:
            await c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
