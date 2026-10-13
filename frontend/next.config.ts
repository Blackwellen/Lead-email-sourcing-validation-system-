import type { NextConfig } from 'next';

/**
 * BACKEND_URL is read when this file is evaluated by `next build`. Next compiles `rewrites()` into
 * `.next/routes-manifest.json` at BUILD time; both `next start` and the standalone `server.js` serve the rewrites from that
 * manifest, so changing BACKEND_URL at runtime has NO effect on the /api proxy. To point a built image at another API host,
 * rebuild with `--build-arg BACKEND_URL=...` (see Dockerfile / README "Backend URL"). `npm run dev` re-reads it on restart.
 */
const backend = (process.env.BACKEND_URL || 'http://localhost:8000').replace(/\/$/, '');

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
        source: '/api/:path*',
        headers: [{ key: 'X-Accel-Buffering', value: 'no' }],
      },
    ];
  },
};

export default nextConfig;
