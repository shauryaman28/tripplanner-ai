# Phase 17 — Frontend: Chat Interface & SSE Streaming

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
├── playwright.config.ts
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

tests/e2e/
└── planning.spec.ts          ← Playwright smoke test (roadmap acceptance criterion)

# Backend — minimal changes only:
src/backend/app/api/routes/trips.py  ← + GET /trips/{id}/status
tests/unit/test_phase17_backend.py   ← 6 tests for the new endpoint
```

## Backend changes (minimal)

One new route added to the existing `trips.py`:

```
GET /trips/{id}/status
→ { status, trip_id, progress: { agents_done, agents_total, agents: {…} } }
```

Derives agent states from the most recent `agent_runs` row per sub-agent —
no new DB columns, no new tables. The query costs 1 index scan on
`ix_agent_runs_trip_id`.

CORS is already `["*"]` in dev mode (`main.py`) — no change needed.
All datetime/UUID serialisation is already handled by Pydantic schemas — no change.

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

### PlanningPhase state machine
```
idle → planning → complete
         ↓
     clarifying → planning → complete
         ↓
       failed
         ↓
       (any complete/failed) → refining → complete
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
- [x] Playwright E2E test: type query → wait for planning_complete → assert day cards
- [x] Login / register flow works
- [x] `GET /trips/{id}/status` endpoint added (SSE polling fallback)
- [x] 6 backend unit tests for the new endpoint
- [x] CORS correct for localhost:3000
- [x] All datetimes ISO 8601, UUIDs as strings (Pydantic handles this already)
- [x] Refinement input appears after planning completes
- [x] Clarification flow handled (clarifying_needed → answer → re-plan)
- [x] Budget conflict options displayed in AgentProgressPanel
- [x] Mobile-responsive layout (stacks to single column below lg breakpoint)
