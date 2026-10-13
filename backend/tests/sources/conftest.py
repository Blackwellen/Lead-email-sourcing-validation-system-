import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LEAD_RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live network probe; set LEAD_RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords and "tests/sources" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)
