/** @type {import('next').NextConfig} */
const nextConfig = {
  // The Playwright stack builds into its own directory (see playwright.config.ts).
  distDir: process.env.NEXT_DIST_DIR ?? ".next",

  // Proxy /api/* → FastAPI backend, so the browser only ever talks to this origin.
  // SSE is the exception: EventSource connects straight to NEXT_PUBLIC_API_URL.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.BACKEND_URL ?? "http://localhost:8000"}/:path*`,
      },
    ];
  },
};

export default nextConfig;
