/**
 * Deterministic radial + relaxation layout for the bounded (≤300 node) business network.
 *
 * 1. Radial seed: the centre sits at (0,0); nodes at graph distance d sit on ring d. Distance-1 nodes are ordered by
 *    (relation, id); deeper nodes are ordered by their parent's angle so subtrees stay together. Crowded rings are split into
 *    staggered sub-rings so neighbouring nodes keep a minimum arc spacing.
 * 2. Relaxation: a fixed number of pairwise-repulsion + edge-spring iterations with NO randomness, the centre pinned and every
 *    node softly pulled back to its ring radius. Same input -> same picture, every time.
 */
export interface LNode { id: number; distance: number }
export interface LEdge { from: number; to: number; relation: string }
export interface Placed { x: number; y: number; ring: number }

const RING = 150;
const MIN_ARC = 26;

export function layoutGraph(nodes: LNode[], edges: LEdge[], center: number, opts: { iterations?: number } = {}): Map<number, Placed> {
  const pos = new Map<number, Placed>();
  if (!nodes.length) return pos;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map<number, { other: number; relation: string }[]>();
  for (const e of edges) {
    if (!byId.has(e.from) || !byId.has(e.to)) continue;
    (adj.get(e.from) ?? adj.set(e.from, []).get(e.from)!).push({ other: e.to, relation: e.relation });
    (adj.get(e.to) ?? adj.set(e.to, []).get(e.to)!).push({ other: e.from, relation: e.relation });
  }
  for (const list of adj.values()) list.sort((a, b) => a.relation.localeCompare(b.relation) || a.other - b.other);

  const maxD = Math.max(...nodes.map((n) => n.distance));
  pos.set(center, { x: 0, y: 0, ring: 0 });
  const angle = new Map<number, number>([[center, 0]]);
  let ringBase = 0;
  for (let d = 1; d <= maxD; d++) {
    const ring = nodes.filter((n) => n.distance === d && n.id !== center);
    const key = (n: LNode) => {
      const parents = (adj.get(n.id) ?? []).filter((a) => (byId.get(a.other)?.distance ?? 99) === d - 1 && angle.has(a.other));
      const pa = parents.length ? angle.get(parents[0].other)! : 0;
      const rel = (adj.get(n.id) ?? [])[0]?.relation ?? '';
      return { pa, rel };
    };
    const keyed = ring.map((n) => ({ n, ...key(n) }));
    keyed.sort((a, b) => (d === 1 ? a.rel.localeCompare(b.rel) : a.pa - b.pa) || a.n.id - b.n.id);
    const radius0 = ringBase + RING;
    const perSub = Math.max(6, Math.floor((2 * Math.PI * radius0) / MIN_ARC));
    const subRings = Math.max(1, Math.ceil(keyed.length / perSub));
    keyed.forEach(({ n }, i) => {
      const a = (2 * Math.PI * i) / keyed.length - Math.PI / 2 + (d % 2 ? 0 : Math.PI / keyed.length);
      const r = radius0 + (i % subRings) * (MIN_ARC * 1.2);
      angle.set(n.id, a);
      pos.set(n.id, { x: r * Math.cos(a), y: r * Math.sin(a), ring: d });
    });
    ringBase = radius0 + (subRings - 1) * MIN_ARC * 1.2;
  }

  // ---- deterministic relaxation (O(n² · iterations); n ≤ 300 keeps this well under a frame budget per iteration)
  const iterations = opts.iterations ?? (nodes.length > 150 ? 40 : 80);
  const ids = nodes.map((n) => n.id);
  const target = new Map(ids.map((id) => { const p = pos.get(id)!; return [id, Math.hypot(p.x, p.y)]; }));
  for (let it = 0; it < iterations; it++) {
    const cool = 1 - it / iterations;
    const disp = new Map<number, { x: number; y: number }>(ids.map((id) => [id, { x: 0, y: 0 }]));
    for (let i = 0; i < ids.length; i++) {
      const a = pos.get(ids[i])!;
      for (let j = i + 1; j < ids.length; j++) {
        const b = pos.get(ids[j])!;
        let dx = a.x - b.x; let dy = a.y - b.y;
        let dist = Math.hypot(dx, dy);
        if (dist < 0.01) { dx = (ids[i] - ids[j]) * 0.01; dy = 0.01; dist = Math.hypot(dx, dy); }
        if (dist > MIN_ARC * 3) continue;
        const f = ((MIN_ARC * 3 - dist) / dist) * 0.5;
        disp.get(ids[i])!.x += dx * f; disp.get(ids[i])!.y += dy * f;
        disp.get(ids[j])!.x -= dx * f; disp.get(ids[j])!.y -= dy * f;
      }
    }
    for (const e of edges) {
      const a = pos.get(e.from); const b = pos.get(e.to);
      if (!a || !b) continue;
      const dx = b.x - a.x; const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 1;
      const f = (dist - RING) / dist * 0.02;
      disp.get(e.from)!.x += dx * f; disp.get(e.from)!.y += dy * f;
      disp.get(e.to)!.x -= dx * f; disp.get(e.to)!.y -= dy * f;
    }
    for (const id of ids) {
      if (id === center) continue;
      const p = pos.get(id)!; const d = disp.get(id)!;
      const step = Math.hypot(d.x, d.y);
      const max = 12 * cool + 1;
      const s = step > max ? max / step : 1;
      let x = p.x + d.x * s; let y = p.y + d.y * s;
      const r = Math.hypot(x, y) || 1; const tr = target.get(id)!;
      x += (x / r) * (tr - r) * 0.15; y += (y / r) * (tr - r) * 0.15; // stay near the node's ring
      pos.set(id, { x, y, ring: p.ring });
    }
  }
  return pos;
}

export function bounds(pos: Map<number, Placed>, pad = 40) {
  let minX = -pad, minY = -pad, maxX = pad, maxY = pad;
  for (const p of pos.values()) { minX = Math.min(minX, p.x - pad); minY = Math.min(minY, p.y - pad); maxX = Math.max(maxX, p.x + pad); maxY = Math.max(maxY, p.y + pad); }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}
