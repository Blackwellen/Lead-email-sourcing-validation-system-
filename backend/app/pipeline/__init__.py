"""Importing this package registers all stage handlers with the worker."""
from . import (
    discovery_stages,  # noqa: F401  (stages: network_expand, gap_recovery, signals, change_check, similar_expand, monitor, completeness, cc_lookup)
    export_handler,  # noqa: F401  (stages: export, retention)
    handlers,  # noqa: E402,F401  (stages: discover, import_domain, resolve_domain, crawl, verify, revalidate)
)
