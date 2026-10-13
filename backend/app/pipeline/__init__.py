"""Importing this package registers all stage handlers with the worker."""
from . import (
    export_handler,  # noqa: F401  (stages: export, retention)
    handlers,  # noqa: E402,F401  (stages: discover, import_domain, resolve_domain, crawl, verify, revalidate)
)
