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
