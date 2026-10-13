"""Per-organisation research completeness (tri-state flags)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..discovery.completeness import FLAGS, compute_completeness
from ..models import Organisation, OrganisationCompleteness
from ..security import ApiError, require_role

router = APIRouter(prefix="/api/organisations", tags=["organisations"], dependencies=[Depends(require_role("viewer"))])


class MissingOpportunity(BaseModel):
    flag: str
    state: str
    classification: str  # incomplete_research | in_progress | genuinely_unavailable | stale
    reason: str
    next_step: str | None


class CompletenessOut(BaseModel):
    organisation_id: int
    flags: dict[str, str]
    reasons: dict[str, str]
    score: int
    research_progress: float
    needs_review: bool
    missing_opportunities: list[MissingOpportunity]
    stored_updated_at: datetime | None
    gap_steps_tried: list[str]
    gap_exhausted: bool
    legend: dict[str, str]


LEGEND = {
    "yes": "established from stored evidence",
    "no": "the research step WAS performed and the thing is genuinely unavailable",
    "unknown": "the research step has not been performed (or produced nothing conclusive)",
    "in_progress": "work is queued / running, or a candidate awaits review",
}


@router.get("/{org_id}/completeness", response_model=CompletenessOut)
async def organisation_completeness(org_id: int, session: AsyncSession = Depends(get_session)) -> CompletenessOut:
    """Live evaluation (read-only; the stored row is refreshed by the worker at the end of each organisation's processing)."""
    if await session.get(Organisation, org_id) is None:
        raise ApiError(404, "not_found", "Business not found.")
    c = await compute_completeness(session, org_id)
    row = await session.get(OrganisationCompleteness, org_id)
    return CompletenessOut(
        organisation_id=org_id, flags={k: c.flags[k] for k in FLAGS}, reasons=c.reasons, score=c.score, research_progress=c.research_progress,
        needs_review=bool(row.needs_review) if row else c.needs_review, missing_opportunities=[MissingOpportunity(**m) for m in c.missing_opportunities],
        stored_updated_at=row.updated_at if row else None, gap_steps_tried=list(row.gap_steps_tried or []) if row else [],
        gap_exhausted=bool(row and row.gap_exhausted_at), legend=LEGEND)


_ = Any
