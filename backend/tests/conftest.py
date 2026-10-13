import os

os.environ.setdefault("LEAD_DATABASE_URL", "postgresql+asyncpg://lead:lead@localhost:5432/lead_test")
os.environ.setdefault("LEAD_ENV", "test")
os.environ.setdefault("LEAD_DATA_DIR", "/tmp/lead-test-var")
os.environ.setdefault("LEAD_EXPORT_DIR", "/tmp/lead-test-var/exports")

import pytest_asyncio
from sqlalchemy import text

from app import db as dbmod
from app.models import Base


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    engine = dbmod.get_engine()
    if os.environ.get("LEAD_TEST_SCHEMA") == "migrations":
        # build the schema ONLY with alembic (proves the migrations are complete: server defaults, indexes ...)
        import subprocess
        import sys

        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        subprocess.run([sys.executable, "-m", "app.cli", "migrate"], check=True, capture_output=True)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    yield
    await dbmod.dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _clean(_schema):
    async with dbmod.sessionmaker()() as s:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await s.commit()
    yield


@pytest_asyncio.fixture
async def session():
    async with dbmod.sessionmaker()() as s:
        yield s
