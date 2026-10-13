"""Worker handlers for the 'export' and 'retention' stages."""
from __future__ import annotations

import logging

from ..exports import run_export
from ..worker import JobContext, handler

log = logging.getLogger("pipeline.export")


@handler("export")
async def handle_export(ctx: JobContext) -> None:
    export_id = int(ctx.job.payload["export_id"])
    ex = await run_export(ctx.session, export_id)
    if ex.status == "failed":
        from ..jobqueue import PermanentError

        raise PermanentError(ex.error or "export failed", "export_failed")


@handler("retention")
async def handle_retention(ctx: JobContext) -> None:
    from ..retention import apply_retention

    result = await apply_retention(ctx.session)
    log.info("retention applied: %s", result)
