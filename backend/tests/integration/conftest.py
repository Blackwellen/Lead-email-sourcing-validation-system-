import pytest

from .helpers import start_server


@pytest.fixture
def web():
    srv = start_server()
    yield srv
    srv.stop()
