import type { NextConfig } from 'next';

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
