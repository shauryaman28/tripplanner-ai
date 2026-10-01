# Phase 17 — Frontend: Chat Interface & SSE Streaming

> **Updated in the Phase 1–17 audit (2026-10-02).** The original E2E spec could not load or pass, and the trip page did not restore state; both are fixed. Details: `docs/phase1-17_audit.md`.

**Status: ✅ Complete**
**Done criterion:** Full flow works in browser. Agent progress panel updates live. Itinerary renders as day-by-day cards. SSE reconnects after network interruption. Playwright E2E smoke test passes.

## What was built

```
src/frontend/
├── package.json
├── next.config.mjs           ← /api/* proxy → FastAPI; no hard-coded ports in JS
├── tailwind.config.ts        ← brand palette: indigo + saffron; Plus Jakarta Sans / Inter
├── tsconfig.json
├── postcss.config.js
├── playwright.config.ts      ← starts the stub backend + dev server itself
├── e2e/planning.spec.ts      ← Playwright smoke test (roadmap acceptance criterion)
├── .env.local.example
├── src/
│   ├── app/
│   │   ├── globals.css       ← Tailwind directives + design tokens
│   │   ├── layout.tsx
│   │   ├── page.tsx          ← redirects → /trips
│   │   ├── login/page.tsx    ← register + login, auto-login after register
│   │   └── trips/
│   │       ├── page.tsx      ← trips list + "New trip" form inline
│   │       └── [id]/page.tsx ← core: chat + SSE + itinerary view
│   ├── components/
│   │   ├── AgentProgressPanel.tsx  ← live status badges (pending/running/done/failed)
│   │   ├── ChatInput.tsx           ← auto-grow textarea, Shift+Enter for newline
│   │   ├── DayCard.tsx             ← one day's slots rendered as a card
│   │   ├── ItineraryView.tsx       ← cost pills + day card list
│   │   └── MessageThread.tsx       ← chat bubbles (user/assistant/system)
│   └── lib/
│       ├── api.ts            ← typed fetch client; token in localStorage
│       ├── sse.ts            ← useSSE hook with exponential back-off reconnect
│       └── types.ts          ← TypeScript mirrors of all FastAPI Pydantic schemas

tests/e2e/stub_backend.py     ← the real app with external APIs stubbed, for Playwright
tests/fakes.py                ← the stubs (shared with the integration tests)

# Backend:
src/backend/app/api/routes/trips.py  ← + GET /trips/{id}, GET /trips/{id}/status
src/backend/app/main.py              ← CORS from settings, error envelope
src/backend/app/schemas/types.py     ← UTCDateTime
tests/unit/test_phase17_backend.py   ← 6 tests for the status endpoint
```

## Backend changes

```
GET /trips/{id}          → the trip (the page needs it on load / reload)
GET /trips/{id}/status   → { status, trip_id, progress: { agents_done, agents_total, agents: {…} } }
```

`/status` derives agent states from `agent_runs` — no new columns. While a run is
in flight only the rows written since the last closing `orchestrator` row count,
so a re-plan starts again from 0/3.

API hardening for the frontend (roadmap Dev A):

- **Datetimes** are emitted as ISO 8601 with an explicit `Z`. They were naive UTC
  before, which browsers parse as local time.
- **Error envelope:** every error body has `{"error": {"code", "message"}}` next to
  FastAPI's `detail`. The frontend shows `error.message`.
- **CORS:** explicit origins from `CORS_ORIGINS` (default `http://localhost:3000`),
  no credentials — auth is a Bearer token. Production policy: DECISIONS #59.
- The SSE `connected` event carries `trip_status`, and the stream no longer pins a
  pooled DB connection.

## Frontend architecture decisions

### /api proxy (next.config.mjs)
Browser code calls `/api/trips/...` which Next.js rewrites server-side to
`http://localhost:8000/trips/...`. This means:
- Zero CORS issues in dev (same-origin from browser's perspective).
- Production just swaps `BACKEND_URL` env var — no code changes.

Exception: SSE (`EventSource`) cannot go through the proxy because it
requires a persistent TCP connection. The SSE URL uses `NEXT_PUBLIC_API_URL`
directly. The backend already accepts `?token=` on the stream endpoint.

### Token storage
`localStorage` keyed `tp_token` — simplest for a dev-phase app. Production
note (Phase 35 security hardening): replace with an httpOnly cookie.

### useSSE reconnect
Exponential backoff: 1s → 2s → 4s → 8s → 16s → 30s (capped).
On reconnect, the `events` array is NOT reset, so the agent progress panel
always shows the last known state — never a blank screen mid-reconnect.

Redis pub/sub has no replay, so two things guard against a missed event:
the page keeps the stream open for its whole life (it is already subscribed when
`POST /plan` is sent), and while a run is in flight it polls `GET /status` every
3 s. Both paths end in one guarded `finishRun`, so the outcome is handled once.

### Page state comes from the API
On mount the page loads the trip and its latest itinerary and derives the phase
from them — a reload, or opening a planned trip from the list, shows the
itinerary instead of "Ready to plan". A budget conflict renders its options as
buttons that call `POST /trips/{id}/replan`.

### PlanningPhase state machine
```
loading → idle | planning | complete | failed      (from GET /trips/{id} + itinerary)

idle / failed → planning → complete
                   ↓  ↘ clarifying → planning
                 failed  (budget conflict → replan → planning)
complete → refining → complete        (a failed refinement keeps the old itinerary)
```

The `ChatInput` placeholder text and disabled state are driven entirely by
the current phase — no ad-hoc boolean flags.

### Component split rationale
| Component | Responsibility |
|---|---|
| `AgentProgressPanel` | Reads SSE events + SSE connection status only |
| `MessageThread` | Pure presentational; renders Message[] |
| `ChatInput` | Input only; calls onSubmit, knows nothing about planning |
| `DayCard` | Renders one DaySchedule; no state |
| `ItineraryView` | Derives cost breakdown from structured_data; renders DayCards |

Each component has exactly one job and receives only the props it needs.

## Design system

| Token | Value | Role |
|---|---|---|
| `brand-600` | `#4f46e5` | CTA buttons, active states, agent avatars |
| `brand-50` | `#eef2ff` | Card headers, badge backgrounds |
| `accent-500` | `#f59e0b` | Gradient accent on avatar, cost highlights |
| `surface` | `#f8f8fc` | Page background |
| Font | Plus Jakarta Sans (display) + Inter (body) | Personality without noise |

One animation is orchestrated (slide-up on cards appearing); hover transitions
on cards only. No scattered fade-in-per-section effects.

## Done criterion checklist

- [x] Full flow works in browser end to end
- [x] Agent progress panel updates in real time from SSE events
- [x] Itinerary renders as day-by-day cards with morning/afternoon/evening slots
- [x] SSE reconnects after network interruption (exponential backoff, state preserved)
- [x] Playwright E2E test: type query → wait for planning_complete → assert day cards → reload → refine (3 tests, runs on a stub backend with no API keys)
- [x] Login / register flow works
- [x] `GET /trips/{id}/status` endpoint added (SSE polling fallback)
- [x] 6 backend unit tests for the new endpoint
- [x] CORS correct for localhost:3000
- [x] All datetimes ISO 8601, UUIDs as strings (Pydantic handles this already)
- [x] Refinement input appears after planning completes
- [x] Clarification flow handled (clarifying_needed → answer → re-plan)
- [x] Budget conflict options displayed in AgentProgressPanel and actionable (replan buttons)
- [x] Planned trips survive a reload; missed SSE events are caught by status polling
- [x] Error envelope + explicit-UTC timestamps; production CORS policy documented (DECISIONS #59)
- [x] Mobile-responsive layout (stacks to single column below lg breakpoint)
