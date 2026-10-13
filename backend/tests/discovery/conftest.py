"""Shared fixtures for discovery tests: local fixture HTTP server + SafeFetcher (no real internet)."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.crawler.safe_http import SafeFetcher
from tests.crawler.conftest import FixtureServer


@pytest.fixture
def server():
    s = FixtureServer().start()
    yield s
    s.stop()


@pytest.fixture
def allow_private(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_private_networks", True)


@pytest.fixture
async def fetcher(allow_private):
    f = SafeFetcher()
    yield f
    await f.aclose()
