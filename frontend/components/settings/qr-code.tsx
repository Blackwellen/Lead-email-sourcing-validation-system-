'use client';
import * as React from 'react';

/** Renders a QR code locally (no external service). Dark modules on a white quiet-zone background in every theme so scanners can read it. */
export function QrCode({ value, label, size = 192 }: { value: string; label: string; size?: number }) {
  const [matrix, setMatrix] = React.useState<{ size: number; get: (r: number, c: number) => number } | null>(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
    let cancelled = false;
    import('qrcode').then((m) => {
      if (cancelled) return;
      try { const q = (m.default ?? m).create(value, { errorCorrectionLevel: 'M' }); setMatrix({ size: q.modules.size, get: (r, c) => q.modules.get(r, c) }); }
      catch { setFailed(true); }
    }).catch(() => setFailed(true));
    return () => { cancelled = true; };
  }, [value]);
  if (failed) return <p className="text-sm text-muted-foreground">The QR code could not be drawn. Use the secret key below instead.</p>;
  if (!matrix) return <div className="animate-pulse rounded-md bg-muted" style={{ width: size, height: size }} aria-hidden />;
  const quiet = 4;
  const n = matrix.size + quiet * 2;
  let d = '';
  for (let r = 0; r < matrix.size; r++) {
    let c = 0;
    while (c < matrix.size) {
      if (!matrix.get(r, c)) { c++; continue; }
      let end = c;
      while (end < matrix.size && matrix.get(r, end)) end++;
      d += `M${c + quiet} ${r + quiet}h${end - c}v1h-${end - c}z`;
      c = end;
    }
  }
  return (
    <svg role="img" aria-label={label} data-testid="mfa-qr" viewBox={`0 0 ${n} ${n}`} width={size} height={size} shapeRendering="crispEdges" className="rounded-md border bg-white">
      <rect width={n} height={n} fill="#ffffff" />
      <path d={d} fill="#000000" />
    </svg>
  );
}
