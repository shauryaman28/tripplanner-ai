/** @type {import('next').NextConfig} */
const nextConfig = {
  // Proxy /api/* → FastAPI backend so the frontend never hard-codes localhost:8000
  // in production, replace NEXT_PUBLIC_API_URL with the Railway URL.
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
