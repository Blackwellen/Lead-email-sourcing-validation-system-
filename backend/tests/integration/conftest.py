import pytest

from .helpers import start_server


@pytest.fixture
def web():
    srv = start_server()
    yield srv
    srv.stop()


@pytest.fixture
def tmp_path():
    """Source files for runs must live inside the uploads directory (the worker re-checks the source-configuration policy
    before running an adapter), so this package's scratch files are created there."""
    import shutil
    import uuid

    from app.sources.access_policy import uploads_root

    p = uploads_root() / f"test-{uuid.uuid4().hex}"
    p.mkdir(parents=True)
    yield p
    shutil.rmtree(p, ignore_errors=True)
