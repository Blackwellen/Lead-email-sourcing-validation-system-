import type { NextConfig } from 'next';

/**
 * BACKEND_URL is read when this file is evaluated by `next build`. Next compiles `rewrites()` into
 * `.next/routes-manifest.json` at BUILD time; both `next start` and the standalone `server.js` serve the rewrites from that
 * manifest, so changing BACKEND_URL at runtime has NO effect on the /api proxy. To point a built image at another API host,
 * rebuild with `--build-arg BACKEND_URL=...` (see Dockerfile / README "Backend URL"). `npm run dev` re-reads it on restart.
 */
const backend = (process.env.BACKEND_URL || 'http://localhost:8000').replace(/\/$/, '');

/**
 * Content-Security-Policy for every page. Notes on the two relaxations:
 *  - script-src 'unsafe-inline': the App Router streams RSC payloads as inline <script> tags and the theme pre-hydration
 *    script in app/layout.tsx is inline. Without nonces (which would force every page to be dynamically rendered) these
 *    cannot load otherwise. No 'unsafe-eval' in production ('unsafe-eval' is added in `next dev` only, for React refresh).
 *  - style-src 'unsafe-inline': Radix (popper positioning), Recharts and the progress/graph components set style attributes.
 * Everything else is same-origin only: the API, SSE and /healthz go through the same-origin rewrite (connect-src 'self').
 */
const isDev = process.env.NODE_ENV !== 'production';
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ''}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${isDev ? ' ws: wss:' : ''}`,
  "object-src 'none'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join('; ');

const nextConfig: NextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  // SSE (/api/runs/{id}/events) must not be gzip-buffered by the Next server.
  compress: false,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${backend}/api/:path*` },
      { source: '/healthz', destination: `${backend}/healthz` },
    ];
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'Content-Security-Policy', value: csp },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
      {
        source: '/api/:path*',
        headers: [{ key: 'X-Accel-Buffering', value: 'no' }],
      },
    ];
  },
};

export default nextConfig;
