import pytest

from .helpers import admin_client, anon_client, app, operator_client, viewer_client  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _reset_maintenance_cache():
    from app.settings_store import invalidate_maintenance_cache

    invalidate_maintenance_cache()
    yield
    invalidate_maintenance_cache()
