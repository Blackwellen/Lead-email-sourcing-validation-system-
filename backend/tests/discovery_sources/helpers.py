from __future__ import annotations

from app.sources.base import DiscoveryQuery


async def collect(adapter, query=None, checkpoint=None, stop_after=None):
    out = []
    async for rec, cp in adapter.iter_businesses(query or DiscoveryQuery(active_only=False), checkpoint):
        out.append((rec, cp))
        if stop_after and len(out) >= stop_after:
            break
    return out


async def assert_resume_no_dup_no_gap(make_adapter, query=None):
    """For EVERY checkpoint position: resuming yields exactly the remaining records (no duplicate, no gap)."""
    q = query or DiscoveryQuery(active_only=False)
    full = await collect(make_adapter(), q)
    assert len(full) >= 2, "need at least 2 records to test resume"
    keys = [r.source_ref for r, _ in full]
    for i in range(len(full) - 1):
        tail = await collect(make_adapter(), q, checkpoint=full[i][1])
        assert [r.source_ref for r, _ in tail] == keys[i + 1:], f"resume after record {i} differs"
    assert await collect(make_adapter(), q, checkpoint=full[-1][1]) == []
    return full
