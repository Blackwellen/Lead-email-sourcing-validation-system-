'use client';
import * as React from 'react';
import { Maximize2, Minus, Plus } from 'lucide-react';
import type { Schema } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { relationMeta } from '../shared';
import { bounds, layoutGraph } from './layout';

type Graph = Schema<'GraphOut'>;
type Node = Schema<'GraphNode'>;

const LABEL_LIMIT = 60;

/**
 * SVG network view. Keyboard: the graph is one tab stop (roving focus over nodes in distance/name order):
 * Arrow keys / Home / End move between nodes, Enter or Space selects the focused node. +/- zoom, 0 resets.
 */
export function GraphView({ graph, selected, onSelect }: { graph: Graph; selected: number | null; onSelect: (id: number) => void }) {
  const pos = React.useMemo(
    () => layoutGraph(graph.nodes.map((n) => ({ id: n.id, distance: n.distance })), graph.edges.map((e) => ({ from: e.from, to: e.to, relation: e.relation })), graph.center),
    [graph],
  );
  const box = React.useMemo(() => bounds(pos), [pos]);
  const order = React.useMemo(() => [...graph.nodes].sort((a, b) => a.distance - b.distance || (a.name ?? '').localeCompare(b.name ?? '') || a.id - b.id), [graph.nodes]);
  const [zoom, setZoom] = React.useState(1);
  const [pan, setPan] = React.useState({ x: 0, y: 0 });
  const [focusIdx, setFocusIdx] = React.useState(0);
  const [hover, setHover] = React.useState<number | null>(null);
  const svgRef = React.useRef<SVGSVGElement>(null);
  const drag = React.useRef<{ x: number; y: number; px: number; py: number; moved: boolean } | null>(null);
  React.useEffect(() => { setZoom(1); setPan({ x: 0, y: 0 }); setFocusIdx(0); }, [graph]);

  const focused = order[focusIdx];
  const active = hover ?? selected ?? focused?.id ?? null;
  const incident = React.useMemo(() => new Set(graph.edges.filter((e) => e.from === active || e.to === active).flatMap((e) => [e.from, e.to])), [graph.edges, active]);
  const showAllLabels = graph.nodes.length <= LABEL_LIMIT;
  const vw = box.w / zoom; const vh = box.h / zoom;
  const vx = box.x + (box.w - vw) / 2 - pan.x; const vy = box.y + (box.h - vh) / 2 - pan.y;
  const relations = Array.from(new Set(graph.edges.map((e) => e.relation))).sort();

  const onKeyDown = (e: React.KeyboardEvent) => {
    const n = order.length;
    if (!n) return;
    if (['ArrowRight', 'ArrowDown'].includes(e.key)) { e.preventDefault(); setFocusIdx((i) => (i + 1) % n); }
    else if (['ArrowLeft', 'ArrowUp'].includes(e.key)) { e.preventDefault(); setFocusIdx((i) => (i - 1 + n) % n); }
    else if (e.key === 'Home') { e.preventDefault(); setFocusIdx(0); }
    else if (e.key === 'End') { e.preventDefault(); setFocusIdx(n - 1); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (focused) onSelect(focused.id); }
    else if (e.key === '+' || e.key === '=') { e.preventDefault(); setZoom((z) => Math.min(6, z * 1.25)); }
    else if (e.key === '-') { e.preventDefault(); setZoom((z) => Math.max(0.4, z / 1.25)); }
    else if (e.key === '0') { e.preventDefault(); setZoom(1); setPan({ x: 0, y: 0 }); }
  };
  const toWorld = (dx: number, dy: number) => {
    const r = svgRef.current?.getBoundingClientRect();
    if (!r) return { x: 0, y: 0 };
    const s = Math.max(vw / r.width, vh / r.height);
    return { x: dx * s, y: dy * s };
  };

  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded-lg border bg-card">
        <div className="absolute right-2 top-2 z-10 flex gap-1">
          <Button size="icon-sm" variant="outline" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(6, z * 1.25))}><Plus /></Button>
          <Button size="icon-sm" variant="outline" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.4, z / 1.25))}><Minus /></Button>
          <Button size="icon-sm" variant="outline" aria-label="Reset view" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}><Maximize2 /></Button>
        </div>
        <svg
          ref={svgRef}
          role="application"
          aria-roledescription="network graph"
          aria-label={`Business network: ${graph.nodes.length} businesses and ${graph.edges.length} relationships. Use arrow keys to move between businesses and Enter to select. The relationships table below lists the same data.`}
          aria-activedescendant={focused ? `gnode-${focused.id}` : undefined}
          tabIndex={0}
          onKeyDown={onKeyDown}
          viewBox={`${vx} ${vy} ${vw} ${vh}`}
          className="block h-[28rem] w-full touch-none select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:h-[34rem]"
          data-testid="network-graph"
          onPointerDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y, moved: false }; }}
          onPointerMove={(e) => {
            const d = drag.current; if (!d) return;
            const w = toWorld(e.clientX - d.x, e.clientY - d.y);
            if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) > 3) d.moved = true;
            setPan({ x: d.px + w.x, y: d.py + w.y });
          }}
          onPointerUp={() => { setTimeout(() => { drag.current = null; }, 0); }}
          onPointerLeave={() => { drag.current = null; }}
          onWheel={(e) => { if (!e.ctrlKey) return; e.preventDefault(); setZoom((z) => Math.max(0.4, Math.min(6, z * (e.deltaY < 0 ? 1.1 : 0.9)))); }}
        >
          <defs>
            {relations.map((r) => (
              <marker key={r} id={`arrow-${r}`} viewBox="0 0 10 10" refX="18" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={relationMeta(r).color} />
              </marker>
            ))}
          </defs>
          <g aria-hidden>
            {graph.edges.map((e) => {
              const a = pos.get(e.from); const b = pos.get(e.to);
              if (!a || !b) return null;
              const m = relationMeta(e.relation);
              const dim = active !== null && e.from !== active && e.to !== active;
              return <line key={e.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={m.color} strokeWidth={dim ? 1 : 2} strokeDasharray={m.dash} strokeOpacity={dim ? 0.25 : 0.9} markerEnd={`url(#arrow-${e.relation})`} data-relation={e.relation} />;
            })}
          </g>
          <g>
            {order.map((n, i) => {
              const p = pos.get(n.id); if (!p) return null;
              const isCenter = n.id === graph.center;
              const isSel = n.id === selected;
              const isFocus = i === focusIdx;
              const near = active !== null && (incident.has(n.id) || n.id === active);
              const r = isCenter ? 13 : 8;
              const label = n.name ?? `#${n.id}`;
              return (
                <g key={n.id} id={`gnode-${n.id}`} role="img" aria-label={`${label}${isCenter ? ' (centre)' : ''}, distance ${n.distance}${n.has_confirmed_domain ? ', confirmed website' : ''}`}
                  transform={`translate(${p.x},${p.y})`} className="cursor-pointer" data-node-id={n.id}
                  onPointerEnter={() => setHover(n.id)} onPointerLeave={() => setHover(null)}
                  onClick={() => { if (drag.current?.moved) return; setFocusIdx(i); onSelect(n.id); }}>
                  {(isSel || isFocus) && <circle r={r + 6} fill="none" stroke="hsl(var(--ring))" strokeWidth={isSel ? 3 : 2} strokeDasharray={isSel ? undefined : '3 2'} />}
                  <circle r={r} fill={isCenter ? 'hsl(var(--primary))' : n.has_confirmed_domain ? 'hsl(var(--card))' : 'hsl(var(--muted))'} stroke={isCenter ? 'hsl(var(--primary))' : 'hsl(var(--foreground))'} strokeWidth={n.has_confirmed_domain ? 2.5 : 1} strokeOpacity={active !== null && !near ? 0.4 : 1} />
                  {n.has_confirmed_domain && !isCenter && <circle r={3} fill="hsl(var(--foreground))" />}
                  {n.merged_into_id && <path d={`M${-r},${-r} L${r},${r}`} stroke="hsl(var(--foreground))" strokeWidth={1.5} />}
                  {(showAllLabels || isCenter || isSel || isFocus || n.id === hover || (near && graph.nodes.length <= 150)) && (
                    <text y={r + 12} textAnchor="middle" className={cn('fill-foreground text-[10px]', isCenter && 'font-semibold')} style={{ paintOrder: 'stroke', stroke: 'hsl(var(--card))', strokeWidth: 3 }}>
                      {label.length > 26 ? `${label.slice(0, 25)}…` : label}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
        </svg>
        <p className="border-t px-3 py-1.5 text-2xs text-muted-foreground" aria-hidden>Drag to pan · Ctrl + wheel or the buttons to zoom · Focus the graph and use arrow keys + Enter</p>
      </div>
      <Legend relations={relations} />
    </div>
  );
}

function Legend({ relations }: { relations: string[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs" data-testid="network-legend">
      <span className="sr-only">Legend:</span>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5" aria-label="Relationship types">
        {relations.map((r) => {
          const m = relationMeta(r);
          return (
            <li key={r} className="inline-flex items-center gap-1.5">
              <svg width="30" height="8" aria-hidden><line x1="0" y1="4" x2="30" y2="4" stroke={m.color} strokeWidth="2.5" strokeDasharray={m.dash} /></svg>
              {m.label} <span className="font-mono text-2xs text-muted-foreground">({m.code})</span>
            </li>
          );
        })}
      </ul>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5" aria-label="Business markers">
        <li className="inline-flex items-center gap-1.5"><svg width="14" height="14" aria-hidden><circle cx="7" cy="7" r="6" fill="hsl(var(--primary))" /></svg>Centre business</li>
        <li className="inline-flex items-center gap-1.5"><svg width="14" height="14" aria-hidden><circle cx="7" cy="7" r="5.5" fill="hsl(var(--card))" stroke="hsl(var(--foreground))" strokeWidth="2" /><circle cx="7" cy="7" r="2" fill="hsl(var(--foreground))" /></svg>Confirmed website (thick ring + dot)</li>
        <li className="inline-flex items-center gap-1.5"><svg width="14" height="14" aria-hidden><circle cx="7" cy="7" r="5.5" fill="hsl(var(--muted))" stroke="hsl(var(--foreground))" strokeWidth="1" /></svg>No confirmed website</li>
        <li className="inline-flex items-center gap-1.5"><span aria-hidden>→</span>Arrow points from the subject to the related business (e.g. branch → parent)</li>
      </ul>
    </div>
  );
}

export type { Node as GraphNodeT };
