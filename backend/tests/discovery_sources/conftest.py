"""Local conftest for discovery-source tests: live probes are skipped unless LEAD_RUN_LIVE=1; no real network otherwise."""
import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LEAD_RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live network probe; set LEAD_RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords and "tests/discovery_sources" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


@pytest.fixture
def nosleep():
    """Records requested sleeps instead of sleeping (back-off / rate-limit tests)."""
    calls: list[float] = []

    async def _sleep(s: float) -> None:
        calls.append(s)

    _sleep.calls = calls  # type: ignore[attr-defined]
    return _sleep
