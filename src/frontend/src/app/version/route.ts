/**
 * GET /version — which commit this build of the site is (Phase 28).
 *
 * The backend answers the same question at its own /version (through this site: /api/version). The
 * pipeline that deploys asks both, and a merge is "live" when both say the merged commit — a build that
 * was started is not yet a site that is being served.
 *
 * Static: the answer is decided when the site is built, like everything else about a build.
 */

import { NextResponse } from "next/server";

export const dynamic = "force-static";

export function GET() {
  return NextResponse.json({ commit: process.env.NEXT_PUBLIC_COMMIT_SHA || null });
}
