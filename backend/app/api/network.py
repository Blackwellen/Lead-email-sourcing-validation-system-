"""Business-network API: neighbourhood graph (hard cap 300 nodes), relationship table and bounded expansion jobs.

An expansion NEVER confirms anything: discovered companies are candidates whose websites stay unverified until the identity
engine decides (see ``discovery/graph.py``)."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from .. import jobqueue as jq
from ..db import get_session
from ..discovery import graph
from ..models import BusinessRelationship, Organisation, OrganisationDomain, User
from ..security import ApiError, audit, require_role
from ..system_runs import ensure_system_run
from .common import Page, SortSpec, apply_keyset, csv_strs, finish_page

router = APIRouter(prefix="/api/network", tags=["network"], dependencies=[Depends(require_role("viewer"))])
RELATIONS = ("branch_of", "brand_of", "subsidiary_of", "supplier_of", "partner_of", "franchise_of", "member_of_group", "exhibits_with", "awarded_by", "same_group")
NODE_CAP = graph.HARD_NODE_CAP
SORTS = {"id": SortSpec(BusinessRelationship.id, "int")}


class GraphNode(BaseModel):
    id: int
    name: str | None
    country_code: str | None
    city: str | None
    distance: int
    has_confirmed_domain: bool
    merged_into_id: int | None


class GraphEdge(BaseModel):
    id: int
    from_: int = Field(alias="from", serialization_alias="from")
    to: int
    relation: str
    depth: int
    confidence: float
    source_adapter: str
    source_url: str | None
    observed_at: datetime

    model_config = {"populate_by_name": True}


class GraphOut(BaseModel):
    center: int
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = Field(description="true when the neighbourhood had more nodes than the (capped) limit")
    node_cap: int = NODE_CAP
    limit_applied: int
    depth_applied: int


class RelationshipOut(BaseModel):
    id: int
    from_org_id: int
    from_name: str | None
    to_org_id: int
    to_name: str | None
    relation: str
    depth: int
    confidence: float
    source_adapter: str
    source_url: str | None
    observed_at: datetime


class ExpandIn(BaseModel):
    max_depth: int = Field(1, ge=1, le=3)
    budget: int = Field(30, ge=1, le=200, description="maximum page fetches for this expansion")


class ExpandOut(BaseModel):
    organisation_id: int
    queued: bool
    job_id: int | None
    run_id: int
    max_depth: int
    budget: int


async def _live_org(session: AsyncSession, org_id: int) -> Organisation:
    o = await session.get(Organisation, org_id)
    if o is None:
        raise ApiError(404, "not_found", "Business not found.")
    return o


@router.get("/organisations/{org_id}/graph", response_model=GraphOut)
async def organisation_graph(org_id: int, depth: int = Query(1, ge=1, le=3), limit: int = Query(100, ge=1, le=1000),
                             relations: str | None = Query(None, description="comma separated relation filter"),
                             session: AsyncSession = Depends(get_session)) -> GraphOut:
    """Neighbourhood around an organisation. ``limit`` is clamped to 300 nodes; ``truncated`` says when nodes were left out."""
    await _live_org(session, org_id)
    rel = csv_strs(relations, "relations", max_items=len(RELATIONS))
    bad = [r for r in rel if r not in RELATIONS]
    if bad:
        raise ApiError(422, "invalid_filter", f"Unknown relation(s) {bad}; allowed: {', '.join(RELATIONS)}.")
    applied = min(limit, NODE_CAP)
    g = await graph.neighbourhood(session, org_id, depth=depth, limit=applied, relations=rel or None)
    truncated = bool(g["truncated"]) or limit > NODE_CAP and len(g["nodes"]) >= NODE_CAP
    return GraphOut(center=org_id, nodes=[GraphNode(**n) for n in g["nodes"]], edges=[GraphEdge(**e) for e in g["edges"]], truncated=truncated,
                    limit_applied=applied, depth_applied=depth)


@router.get("/relationships", response_model=Page[RelationshipOut])
async def list_relationships(
    relation: str | None = Query(None, max_length=24), org_id: int | None = Query(None, ge=1), source_adapter: str | None = Query(None, max_length=80),
    min_confidence: float | None = Query(None, ge=0, le=1), limit: int = Query(50, ge=1, le=500), cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> Page[RelationshipOut]:
    if relation and relation not in RELATIONS:
        raise ApiError(422, "invalid_filter", f"'relation' must be one of: {', '.join(RELATIONS)}.")
    fo, to = aliased(Organisation), aliased(Organisation)
    R = BusinessRelationship
    q = (select(R, func_name(fo).label("fn"), func_name(to).label("tn"), R.id.label("_sv"), R.id.label("_id"))
         .join(fo, fo.id == R.from_org_id).join(to, to.id == R.to_org_id))
    if relation:
        q = q.where(R.relation == relation)
    if org_id:
        q = q.where((R.from_org_id == org_id) | (R.to_org_id == org_id))
    if source_adapter:
        q = q.where(R.source_adapter == source_adapter)
    if min_confidence is not None:
        q = q.where(R.confidence >= min_confidence)
    q = apply_keyset(q, SORTS, "id", "desc", R.id, cursor, limit)
    rows, nxt = finish_page((await session.execute(q)).all(), "id", "desc", limit)
    items = [RelationshipOut(id=r[0].id, from_org_id=r[0].from_org_id, from_name=r.fn, to_org_id=r[0].to_org_id, to_name=r.tn, relation=r[0].relation,
                             depth=r[0].depth, confidence=r[0].confidence, source_adapter=r[0].source_adapter, source_url=r[0].source_url,
                             observed_at=r[0].observed_at) for r in rows]
    return Page[RelationshipOut](items=items, next_cursor=nxt)


def func_name(o):
    from sqlalchemy import func

    return func.coalesce(o.legal_name, o.trading_name)


@router.post("/organisations/{org_id}/expand", response_model=ExpandOut, status_code=202)
async def expand_organisation(org_id: int, body: ExpandIn | None = None, user: User = Depends(require_role("operator")),
                              session: AsyncSession = Depends(get_session)) -> ExpandOut:
    """Queue a bounded ``network_expand`` job (system run). Needs an organisation with a confirmed official domain."""
    body = body or ExpandIn()
    o = await _live_org(session, org_id)
    if o.merged_into_id is not None:
        raise ApiError(409, "merged", f"This business was merged into #{o.merged_into_id}; expand that one.")
    has = (await session.execute(select(OrganisationDomain.id).where(
        OrganisationDomain.organisation_id == org_id, OrganisationDomain.verified_match_status.in_(("confirmed", "high")),
        OrganisationDomain.parked.is_(False)).limit(1))).first()
    if has is None:
        raise ApiError(409, "no_confirmed_domain", "Network expansion needs a confirmed official website for this business.")
    rid = await ensure_system_run(session, "network-expansion")
    jid = await jq.enqueue(session, run_id=rid, stage="network_expand", dedupe_key=f"api:{org_id}:{body.max_depth}:{body.budget}:{int(time.time() // 60)}",
                           payload={"organisation_id": org_id, "max_depth": body.max_depth, "budget": body.budget, "force": True}, priority=60)
    await audit(session, user, "network.expand", "organisation", org_id, {"max_depth": body.max_depth, "budget": body.budget, "job_id": jid})
    await session.commit()
    return ExpandOut(organisation_id=org_id, queued=jid is not None, job_id=jid, run_id=rid, max_depth=body.max_depth, budget=body.budget)


_ = (text, Any)
