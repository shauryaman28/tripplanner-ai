/** @type {import('next').NextConfig} */

// Where the backend is. Both are read when the site is BUILT, not when it runs:
//   BACKEND_URL          the destination of the /api/* rewrite below — the server's side;
//   NEXT_PUBLIC_API_URL  where the browser opens the event stream — compiled into the page's script.
// On a laptop both default to http://localhost:8000. On a host that builds for the public (Vercel sets
// VERCEL=1) a default would be a site that builds, deploys and then sends every request to its own
// localhost — so there the build stops, and says which one is missing (Phase 27).
const backendUrl = (process.env.BACKEND_URL ?? "").replace(/\/+$/, "");
const publicApiUrl = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

if (process.env.VERCEL === "1") {
  const wrong = [];
  for (const [name, value] of [
    ["BACKEND_URL", backendUrl],
    ["NEXT_PUBLIC_API_URL", publicApiUrl],
  ]) {
    if (!value) wrong.push(`${name} is not set`);
    else if (!/^https:\/\//.test(value)) wrong.push(`${name} must be an https:// address`);
    else if (/\/\/(localhost|127\.0\.0\.1)([:/]|$)/.test(value)) wrong.push(`${name} points at localhost`);
  }
  if (wrong.length > 0) {
    throw new Error(
      `Cannot build for deployment: ${wrong.join("; ")}. ` +
        "Set both to the backend's public address (Project → Settings → Environment Variables) and deploy again.",
    );
  }
}

const nextConfig = {
  // The Playwright stack builds into its own directory (see playwright.config.ts).
  distDir: process.env.NEXT_DIST_DIR ?? ".next",

  // Which commit this build is of — compiled in, and answered at /version (Phase 28). Vercel says it when
  // it builds from GitHub; anywhere else GIT_COMMIT_SHA can; a laptop's build has none.
  env: {
    NEXT_PUBLIC_COMMIT_SHA: process.env.VERCEL_GIT_COMMIT_SHA ?? process.env.GIT_COMMIT_SHA ?? "",
  },

  // Proxy /api/* → FastAPI backend, so the browser only ever talks to this origin.
  // SSE is the exception: EventSource connects straight to NEXT_PUBLIC_API_URL.
  //
  // On Vercel this rewrite is carried out by its proxy, not by a function: no request the page makes
  // runs as code that could time out. The proxy waits two minutes for the backend to begin to answer,
  // and planning never makes it wait — POST /plan answers at once, and the progress is the stream.
  // Phase 35: Next's image optimizer (/_next/image) is on in every app unless it is switched off, and the
  // version this site is on has advisories against it that only Next 15.5 fixes (DECISIONS #322). Nothing here
  // uses next/image: off, and the route is gone.
  images: { unoptimized: true },

  // Phase 35: the site's own pages say what the API's answers say — no frames (clickjacking), no sniffing,
  // no referrer beyond the origin, no camera or location. Its policy says where it may be framed and what
  // a page may base or post to; which scripts may run is not restricted (Next's own inline scripts would
  // need a nonce per request — docs/phase35_build_log.md, Known limits). /api/* is the backend's, which
  // sets its own.
  async headers() {
    return [
      {
        source: "/((?!api/).*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
          {
            key: "Content-Security-Policy",
            value: "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'",
          },
        ],
      },
    ];
  },

  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl || "http://localhost:8000"}/:path*`,
      },
    ];
  },
};

export default nextConfig;
