# How to Run & Verify — Phases 1–22

## What changed vs the original codebase?

| Phase | Existing files touched | New files added |
|---|---|---|
| 1 | 0 (untouched) | 0 |
| 2 | 0 (untouched) | 0 |
| 3 | `tools.py` replaced, `models.py` +2 fields, `pytest.ini` +1 line | `config.py`, `cache.py` |
| 4 | `models/__init__.py` filled in | 5 model files, full Alembic setup |
| 5 | `main.py` +2 routers | `security.py`, `deps.py`, `auth.py`, `trips.py`, 4 schema files |
| 6 (Dev A) | `requirements.txt` +1 line (`langgraph`) | `agents/flight_agent.py`, `mcp_client/client.py` (stub) + `__init__.py`, `tests/unit/test_flight_agent.py` |
| — (fixes) | `docker-compose.yml`, `requirements.txt`, `README.md`, 5 model/security files (`timezone`-aware `datetime`) | 0 |
| 7A | `flight_agent.py` (single → 3-node graph) | `test_flight_agent_router.py` (8 tests), `flight_agent_v2.md`, `flight_agent_v3.md` |
| 7B | `trips.py` (rewired plan, added clarify), `trip.py` (+2 schemas), `flight_agent.py` (+2 lines) | `conversation.py`, `test_phase7b_clarification.py`, `DECISIONS.md` |
| 8–12 | `orchestrator.py`, `builder.py`, `evaluator.py`, `budget_decision.py` | `hotel_agent.py`, `activities_agent.py`, tests |
| 13–16 | `agent_runs`, `orchestrator.py`, `models.py`, `tools.py` | `preference_extractor.py`, `user_preferences.py`, migrations 002 & 003, unit tests |
| 17 | `trips.py` (+ `GET /trips/{id}/status`) | Next.js 14 frontend in `src/frontend/`, `tests/unit/test_phase17_backend.py`, `src/frontend/e2e/planning.spec.ts` |
| 18 | `tools.py`, `models.py`, `builder.py`, `orchestrator.py`, `trips.py` (`budget_conflict` in `/status`), `main.py`, every frontend page and component — see `docs/phase18_build_log.md` | `src/ai/itinerary.py`, `ItineraryMap.tsx`, `CostSummary.tsx`, `AppHeader.tsx`, `Brand.tsx`, `ui.tsx`, `lib/map.ts`, `lib/places.ts`, `lib/format.ts`, `tests/database.py`, `tests/unit/test_phase18_map.py` |
| 19 | `trips.py` (`GET /trips/{id}/export/pdf`), `deps.py`, `config.py` (`MAP_TILE_URL`), `main.py` (CORS exposes the download's headers), `requirements.txt` (`reportlab`, `pillow`), `ItineraryView.tsx`, `lib/api.ts`, `playwright.config.ts`, `tests/fakes.py` — see `docs/phase19_build_log.md` | `src/backend/app/pdf/` (`export.py`, `plan.py`, `static_map.py`, `document.py`, `flowables.py`, `formatting.py`, `theme.py`, `fonts/`), `DownloadPdfButton.tsx`, `Toast.tsx`, `lib/download.ts`, `tests/unit/test_phase19_pdf.py` |
| 20 | `builder.py` (streams), `orchestrator.py` (`builder_token`, `retry_search`), `conversation.py`, `trips.py` (`POST /trips/{id}/retry`; `/status` gains `errors`, `retryable`, `run`; `filename*`), `requirements.txt` (`reportlab[shaping]`), `document.py`, `flowables.py`, `build_fonts.py`, the trip page and its components, `tests/e2e/stub_backend.py` — see `docs/phase20_build_log.md` | `src/ai/utils/failures.py`, `src/backend/app/pdf/scripts.py`, `pdf/fonts/noto/`, `LiveDraft.tsx`, `ProgressSheet.tsx`, `TripStages.tsx`, `lib/draft.ts`, `lib/useWideScreen.ts`, `tests/unit/test_phase20_streaming.py`, `tests/unit/test_phase20_scripts.py`, `e2e/draft.spec.ts`, `e2e/polish.spec.ts` |
| 21 | `tools.py` + `models.py` (`estimate_budget`: destination, month, nights → a range and the season), `evaluator.py`, `orchestrator.py` (a conflict priced; a trip that fits goes ahead; a picked way out is gone ahead with), `trips.py` (`/replan` applies the option as offered; `/status` returns its estimate), `TripStages.tsx`, the trip page, `types.ts`, `tests/e2e/stub_backend.py` — see `docs/phase21_build_log.md` | `src/ai/pricing.py`, `src/ai/agents/budget_alternatives.py`, `tests/unit/test_phase21_budget.py`, `e2e/budget.spec.ts` |
| 22 | `orchestrator.py` (the fourth agent beside the hotel and activities searches; tips carried by refinements), `builder.py` (tips attached to the checked draft), `itinerary.py`, `pdf/plan.py` + `document.py` (a Local tips page), `ItineraryView.tsx`, `lib/changes.ts`, `lib/types.ts`, `tests/fakes.py`, `tests/conftest.py`, `tests/e2e/stub_backend.py` — see `docs/phase22_build_log.md` | `src/ai/agents/destination_intelligence.py`, `prompts/destination_intelligence_v1.md`, `LocalTips.tsx`, `lib/tips.ts`, `tests/unit/test_phase22_intelligence.py`, `e2e/tips.spec.ts` |
| 1–17 audit | most of `src/ai`, `trips.py`, `main.py`, the trip page — see `docs/phase1-17_audit.md` | `src/ai/llm.py`, `routes/admin.py`, `tests/fakes.py`, `tests/e2e/stub_backend.py`, pipeline + schema integration tests |


The notable rewrites: `tools.py` (mocks → real APIs, then Amadeus → Duffel for flights and LiteAPI for hotels),
`orchestrator.py` and `trips.py` (audit), and `main.py` (routers, CORS, error envelope, startup recovery).

---

## Step 0 — First-Time Mac Setup (skip if already done)

If you're starting on a completely fresh Mac, do this first.

### Check Python version
```bash
python3 --version
```
3.11+ matches CI/Docker exactly; 3.9+ also works fine for local dev.

### Install Docker Desktop (required — there is no way around this)
```bash
open "https://www.docker.com/products/docker-desktop/"
```
Download the version matching your chip (`uname -m` → `arm64` = Apple Silicon, `x86_64` = Intel). Install it like a normal Mac app (drag to Applications), then launch it once:
```bash
open -a Docker
```
Wait until the whale icon in your menu bar stops animating. Confirm:
```bash
docker --version
docker compose version
```

### (Optional) Install Homebrew
Not required for anything in this guide — `psql` commands below use `docker exec` instead so you don't need a separate Postgres client installed. Only bother with Homebrew if you want `psql`/`brew` for other reasons:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Create and activate your virtual environment
```bash
cd tripplanner-ai
python3 -m venv .venv
source .venv/bin/activate
```
Your prompt should now show `(.venv)`. Every command below assumes this is active — if a command says `command not found` for something you know you installed, this is almost always why (wrong terminal tab, venv not active).

---

## Step 1 — Setup

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```bash
openssl rand -hex 32
```
Copy that output into:
```env
JWT_SECRET=<paste generated value here>
```

Everything else can stay as the defaults for local dev. `.env` always lives in the
**repo root** — the backend, Alembic and the MCP server all read it from there,
whatever directory you start them from.

To plan a real trip, fill in the five keys listed in `.env.example`:
`GOOGLE_API_KEY` (Gemini), `GROQ_API_KEY`, `DUFFEL_ACCESS_TOKEN` (flights),
`LITEAPI_API_KEY` (hotels) and `OPENTRIPMAP_API_KEY` (attractions). All have a free
tier. Without a key that tool returns a structured `ToolError` instead of crashing,
and the trip is planned from whatever data is available.

No keys at all? Steps 15–16 run the whole flow on a stub backend.

---

## Step 2 — Install dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

`requirements.txt` already includes fixes for three issues discovered during first-time setup (see "Known first-run issues" below) — if you're on an older clone missing these, see that section.

Everything installs with `pip` alone. The PDF export (Phase 19) uses ReportLab and Pillow, which need
no system libraries — nothing to `brew install` or `apt-get`.

---

## Step 3 — Start Docker infrastructure

```bash
docker compose up postgres redis -d
```

Wait ~10 seconds, then verify both services are healthy:

```bash
docker compose ps
# postgres → healthy
# redis    → healthy
```

---

## Step 4 — Run Alembic migrations ✅ Phase 4 check

Run from the **project root** (where `alembic.ini` lives):

```bash
alembic upgrade head
```

Expected output:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema — users, trips, itineraries, agent_runs, embeddings
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, Add turn column to agent_runs — Phase 15 multi-turn refinement
INFO  [alembic.runtime.migration] Running upgrade 002 -> 003, Add user_preferences table — Phase 16 personalisation
```

Verify the tables exist (via the container — no local `psql` install needed):

```bash
docker exec -it tripplanner_postgres psql -U tripplanner -d tripplanner_db -c "\dt"
```

You should see: `agent_runs`, `embeddings`, `itineraries`, `trips`, `user_preferences`, `users`, and `alembic_version` (Alembic's own bookkeeping table — not one you created).

If `alembic upgrade head` prints nothing but `\dt` shows no tables, the version table is ahead of the schema. Reset it and migrate again: `alembic stamp base && alembic upgrade head`.

---

## Step 5 — Run the backend

```bash
cd src/backend
uvicorn app.main:app --reload
```

Leave this running in its own terminal tab. Do the remaining steps in a **second** tab (with `.venv` activated there too).

To also auto-reload on changes under `src/ai`, start it from the repo root instead:

```bash
uvicorn app.main:app --reload --app-dir src/backend --reload-dir src
```

Or run it in Docker (migrations run on start): `docker compose up backend --build`. The `--build`
matters after pulling a change to `requirements.txt`: an image built earlier does not have the new
packages, and the backend stops on import.

---

## Step 6 — Verify Phase 1: Health check

```bash
curl http://localhost:8000/ping
```

Expected (both services healthy):

```json
{"postgres": "ok", "redis": "ok"}
```

`/ping` always returns **200**. If a service is down the error appears in the body, not the
HTTP status — so callers never need to handle two different response shapes:

```json
{"postgres": "error: could not connect to server", "redis": "ok"}
```

To trigger this deliberately: `docker compose stop postgres`, run the curl, then
`docker compose start postgres`.

---

## Step 7 — Verify Phase 5: Auth

### Register

```bash
curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"testpass123"}' \
  | python3 -m json.tool
```

Expected: `201` with `{ "id": "...", "email": "test@test.com", "created_at": "..." }`

Try registering the same email again — you should get `400 Email already registered.`

### Login and grab the token

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -d "username=test@test.com&password=testpass123" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo "Token: ${TOKEN:0:40}..."
```

### Verify JWT enforcement

```bash
# No token → 401
curl -s http://localhost:8000/trips \
  -o /dev/null -w "Status: %{http_code}\n"

# Bad token → 401
curl -s http://localhost:8000/trips \
  -H "Authorization: Bearer notavalidtoken" \
  -o /dev/null -w "Status: %{http_code}\n"
```

---

## Step 8 — Verify Phase 5: Trip routes

### List trips (empty at first)

```bash
curl -s http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
# → 200 []
```

### Create a trip

```bash
TRIP_ID=$(curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "destination": "Goa",
    "start_date": "2027-12-10",
    "end_date": "2027-12-17",
    "budget": 50000,
    "interests": ["beach", "food"]
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Trip ID: $TRIP_ID"
```

Expected: `201` with the full trip object, `status: "pending"`. A `start_date` in the past, or an
`end_date` that is not after it, is a `422`.

Every error body has the same envelope next to FastAPI's `detail`:
`{"detail": ..., "error": {"code": "VALIDATION_ERROR", "message": "start_date: ..."}}`.

---

## Step 9 — Verify Phase 5: SSE stream

Open **two terminals**.

**Terminal 1 — subscribe to the stream:**

```bash
curl -N "http://localhost:8000/trips/$TRIP_ID/stream?token=$TOKEN"
```

**Terminal 2 — publish a fake agent event:**

```bash
docker exec tripplanner_redis redis-cli \
  PUBLISH "trip:$TRIP_ID:events" \
  '{"agent":"flight_agent","status":"completed","summary":"Found 3 flights from DEL to GOI"}'
```

Terminal 1 first prints a `connected` event (with the trip's current status), then the
published event within milliseconds.

---

## Step 10 — Run all unit and contract tests ✅ Phases 1–22 check

Run from the **project root**:

```bash
pytest tests/unit/ tests/contract/ -v
```

Expected: **675 passed**, no network, no Docker. The Phase 19 and 20 tests build real PDFs and read them back.

Integration tests (need Docker Postgres + Redis running):

```bash
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

Expected: **23 passed**. They run against a separate `tripplanner_db_test` database
(created automatically, migrated with Alembic), so they never touch your dev data.
`test_pipeline_integration.py` is the one to watch: it drives plan → PDF export → refine →
add-day, budget conflict → replan, and a no-provider run through the HTTP API with real
Postgres, Redis and the real graph — only the external APIs and the map tiles are stubbed.

---

## Step 11 — Verify Phase 3: MCP tools

```bash
python -m src.ai.mcp_server.server
```

With API keys added to `.env`, use MCP Inspector to call all 5 tools interactively:

```bash
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

---

## Step 12 — Swagger UI (easiest full check)

With the backend running, open **http://localhost:8000/docs**

---

## Step 13 — Verify Phase 6: FlightAgent (Dev A) + MCP client (Dev B)

### Dev A — FlightAgent (works standalone, no live infra needed)

```bash
pytest tests/unit/test_flight_agent.py -v
```

Expected: 6 passed (4 happy path, 2 error cases). All mocks — no network, no Docker required.

### Dev B — real MCP client smoke test

```bash
python3 -c "
import asyncio
from src.ai.mcp_client.client import call_tool

async def main():
    result = await call_tool('estimate_budget', {'flights': 5000, 'hotels': 2000, 'days': 3, 'daily_spend': 1500})
    print(result)

asyncio.run(main())
"
```

Expected: a dict with `total`, `flights`, `hotels`, etc. — no crash, no hang.

### Dev B — integration test (agent + logger + real DB row)

```bash
RUN_INTEGRATION=1 pytest tests/integration/test_phase6_integration.py -v
```

Expected: 1 passed — confirms `FlightAgent.run()` + `log_agent_run()` together write exactly one correctly-shaped row to `agent_runs`.

---

## Step 14 — Verify Phase 7: Router & clarification flow

### Router unit tests (8 tests)

```bash
pytest tests/unit/test_flight_agent_router.py -v
```

Expected: 8 passed — 5 router tests, 2 clarify_node tests, 1 intent_parsing pass-through. All deterministic, LLM mocked.

### Clarification API tests (5 tests)

```bash
pytest tests/unit/test_phase7b_clarification.py -v
```

Expected: 4 passed — structured plan, plan with free text, clarify starts planning, auth required.
(The `clarification_needed` branch itself is covered in `test_pipeline_regressions.py`.)

### Manual verification (requires backend running + Docker)

A trip row always has a destination, dates and a budget, so the one thing that can be missing is
what the traveller enjoys. With no interests and no free text, `/plan` asks instead of assuming:

```bash
TRIP_ID=$(curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2027-12-10","end_date":"2027-12-17","budget":50000}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# No interests, no raw_input → 200 clarification_needed (nothing is started)
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/plan" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Answer it → planning_started; the answer is parsed as free text
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/clarify" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"answer": "history and beaches"}' | python3 -m json.tool
```

---

## Step 15 — Verify Phases 8–16: the full pipeline over HTTP

No API keys needed — run the stub backend instead of Step 5 (stop the real one first).
It is the real app, Redis and graph; only the external APIs are faked. It uses its own
`tripplanner_db_e2e` database, emptied on every start, so your dev data is untouched:

```bash
PORT=8000 python -m tests.e2e.stub_backend      # from the project root (default port: 8100)
```

Then, with `$TOKEN` from Step 7 (register/login again if you restarted on a fresh DB):

```bash
TRIP_ID=$(curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2027-12-10","end_date":"2027-12-15","budget":50000,"group_size":2,"interests":["beach"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Terminal 1 — watch the live events
curl -N "http://localhost:8000/trips/$TRIP_ID/stream?token=$TOKEN"

# Terminal 2 — plan
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/plan" -H "Authorization: Bearer $TOKEN"
```

Terminal 1 shows, in order: `planning_started` → `flight_agent` → `hotel_agent` + `activities_agent`
(concurrent) → `planning_complete`.

| Phase | Check | Expected |
|---|---|---|
| 13 / 17 | `curl -s localhost:8000/trips/$TRIP_ID/status -H "Authorization: Bearer $TOKEN"` | `"status":"completed"`, `agents_done: 3` |
| 12 | `…/trips/$TRIP_ID/itinerary` | day-by-day `structured_data`, `total_cost` |
| 13 | `…/trips/$TRIP_ID/runs` | 11 rows in order: `intent_parsing`, `preferences`, `flight_agent`, `budget_decision`, `hotel_agent` / `activities_agent` (concurrent, either order), `itinerary_builder`, `evaluator`, `persist`, `preference_extractor`, `orchestrator` — each with `duration_ms` |
| 13 | `…/trips/$TRIP_ID/timeline` and `…/trips?status=completed` | readable ordered log; only completed trips |
| 14 | `…/admin/embedding-health` | `{"status":"ok","in_flight":0,"pending_retry":0}`; `SELECT count(*), max(vector_dims(vector)) FROM embeddings` → `2`, `1536` |
| 15 | `curl -s -X POST …/trips/$TRIP_ID/refine -H … -d '{"message":"closer to the beach"}'` then `…/runs?turn=2` and `…/itineraries` | `turn: 2`; turn-2 rows are `hotel_agent`, `itinerary_builder`, `evaluator`, `persist`, `orchestrator` (no `flight_agent`); two itinerary versions, newest first |
| 16 | `curl -s -X PUT localhost:8000/users/preferences -H … -d '{"dietary_restrictions":["vegetarian"],"home_city":"Mumbai"}'`, then plan a new trip | the `activities_agent` run's `input.interests` contains `vegetarian`; the `flight_agent` run's `input.origin` is `BOM` |
| 10 | create a trip with `"budget": 9000`, plan it | `budget_conflict` then `planning_failed` on the stream; trip `failed`; `POST …/replan` with `{"choice":"increase_budget"}` raises the trip's budget by 25% and re-plans |
| 13 | `POST …/plan` twice in a row | the second is `409` while the first run is in flight |

---

## Step 16 — Verify Phase 17: frontend + browser E2E

```bash
cd src/frontend
npm install
npm run dev            # http://localhost:3000
```

Register → **Plan a trip** → fill the form → on the trip page describe the trip (or tap a
suggestion). The assistant panel shows the three searches finishing live; on `planning_complete`
the cost card, the day cards and the map appear and the input switches to "Ask for a change…".
Things to try:

- Click a pin → popup. Click a day in the map legend → only that day. Click a stop's **Map**
  button in a day card → the map scrolls into view with that pin open.
- Ask for a change ("Switch to a nicer hotel") → the plan dims while it updates; only that search
  runs again, and the assistant replies with what changed ("Stay: A → B (₹8,014 → ₹11,766 a night)")
  and how the total moved. The stops stay where they were. "Add some forts" adds places without
  removing the others; "Make it cheaper" with nothing cheaper says the plan came out the same.
- Create a trip with a budget the flights eat up (e.g. ₹12,000) → "Over budget" with three options.
  Reload — the options are still there.
- Narrow the window to phone width → the plan comes first, with an "Ask for a change" button.
- Reload the page: the itinerary is still there.

Optional: `NEXT_PUBLIC_MAP_TILE_URL` / `NEXT_PUBLIC_MAP_ATTRIBUTION` in `src/frontend/.env.local`
switch the map to another tile provider (default: OpenStreetMap).

Automated — Playwright starts its own stack (stub backend on :8100 with the `tripplanner_db_e2e`
database, Next.js on :3100), so it can run while the dev servers are up:

```bash
cd src/frontend
npx playwright install chromium     # once
npx playwright test                 # 47 passed
```

Static checks, from the same directory: `npx tsc --noEmit && npm run lint` — both clean.

Playwright runs `python -m tests.e2e.stub_backend` on the repo's `.venv` (active or not; set
`PYTHON=/path/to/python` for another interpreter). Needs Docker Postgres + Redis. The spec asserts
the stub's data, so it is not meant to run against real API keys — use the manual steps above for that.

---

## Step 17 — Verify Phase 19: PDF export

With the backend running and a planned trip (`$TRIP_ID` from Step 15, `$TOKEN` from Step 7):

```bash
curl -s -D - -o trip.pdf "http://localhost:8000/trips/$TRIP_ID/export/pdf" \
  -H "Authorization: Bearer $TOKEN" | grep -iE "^(HTTP|content-type|content-disposition|x-itinerary-map)"
open trip.pdf      # macOS; any PDF viewer will do
```

Expected:

```
HTTP/1.1 200 OK
content-disposition: attachment; filename="trip-goa-2027-12-10.pdf"
x-itinerary-map: included
content-type: application/pdf
```

The PDF has four sections: a cover (destination, dates, total against the budget), the days
(flight, morning / afternoon / evening, the stay, each day's cost), a cost breakdown (flights, stay,
activities — it adds up to the total), and the map with the same day-coloured, numbered pins as
the web map. The first export of a place fetches its map tiles (about a second); they are then
kept in Redis for a week.

| Check | Expected |
|---|---|
| The same request without the token | `401` |
| A trip that has not been planned yet | `404` — "No itinerary has been generated for this trip yet." |
| Another user's trip | `404` — "Trip not found." |
| Refine the trip, export again | the PDF shows the new plan (it is always the latest version) |

In the browser: open a planned trip → **Download PDF**, beside the "Day by day" heading. The button
reads "Preparing PDF…" while the file is built, then the browser saves `trip-<destination>-<date>.pdf`.

No tile server needs configuring: the default is OpenStreetMap's. `MAP_TILE_URL` in `.env` points
the PDF's map at another provider (same template as the frontend's `NEXT_PUBLIC_MAP_TILE_URL`).

---

## Step 18 — Verify Phase 20: frontend polish

With the backend and the frontend running (Steps 5 and 16):

**The itinerary, as it is written.** Plan a trip and watch the assistant's column: after the three
searches a fourth step, "Itinerary — Writing", says where the writing has got to, and the plan
area shows the days and places filling in under "Writing your itinerary…" — then the checked day
cards replace it. On the stream (Step 9's `curl -N`) the writing is a run of events like

```
data: {"event": "builder_token", "agent": "itinerary_builder", "token": "{\n \"days\": [\n  {…", "seq": 0}
data: {"event": "builder_token", "agent": "itinerary_builder", "token": "…", "seq": 1}
```

about 20 a second, from `seq` 0, all before `planning_complete`.

**A change.** Under the plan the composer reads "Refine this trip:". Ask "Switch to a nicer hotel":
only the stay rows and the stay tile pulse "Updating…", and when the plan comes back what changed
is tinted green and labelled "Updated" for a few seconds. The flights and the stops never move.

**A search that failed.** Remove `LITEAPI_API_KEY` from `.env`, restart the backend and plan a trip:
the plan comes without a stay, and the panel says "This search is not set up on this server" — with
no Retry beside it, because a second try would fail the same way. A search that failed for a reason
that can pass (the provider was down or slow) has a **Retry**, which runs that one search again and
keeps the rest of the plan. Put the key back, restart, and run the retry from the API:

```bash
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/retry" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"agent": "hotel_agent"}'
# {"status":"retry_started", ..., "refinement_type":"targeted_hotel","agent":"hotel_agent"}
curl -s "http://localhost:8000/trips/$TRIP_ID/status" -H "Authorization: Bearer $TOKEN"
# progress.errors / progress.retryable say why each failed search failed, and whether to offer a retry
```

**On a phone.** In the browser's device toolbar pick a 375 px screen: nothing scrolls sideways; while
a plan is being made its progress is a bar at the foot of the screen that opens to the full panel.

**The PDF in another script.** Create a trip to "गोवा" (or "वाराणसी"), plan it and download the PDF:
the cover and the head of every page say "गोवा" in Devanagari, and the file is saved as
`trip-गोवा-<date>.pdf`:

```bash
curl -s -D - -o trip.pdf "http://localhost:8000/trips/$TRIP_ID/export/pdf" \
  -H "Authorization: Bearer $TOKEN" | grep -i content-disposition
# content-disposition: attachment; filename="trip-<date>.pdf"; filename*=UTF-8''trip-%E0%A4%97%E0%A5%8B%E0%A4%B5%E0%A4%BE-<date>.pdf
```

The flight search fails for such a destination — airports are looked up by English name (Phase 36
will resolve a city in any language) — and the page says to write it in English to include flights.

---

## Step 19 — Verify Phase 21: smarter budget intelligence

**The estimate.** With the venv active, from the repo root:

```bash
python -c "
from src.ai.mcp_server.tools import estimate_budget
from src.ai.mcp_server.models import BudgetInput
print(estimate_budget(BudgetInput(flights=8000, hotels=3000, days=5, daily_spend=2000, destination='Goa', month=12)))"
```

Expected: `total=33000.0 … total_min=26400.0 total_max=39600.0 season='peak' season_multiplier=1.4
off_peak_months=[6, 7, 8, 9] off_peak_total=23571.4…` — December is peak season in Goa, about 40% above
the monsoon, so the total may land anywhere within ±20% by the time it is booked.

**A conflict, priced.** In the browser, plan a trip whose flights eat the budget — for two people, Goa,
three or four days about six weeks out, on about ₹35,000 (the sandbox's return fare for two from Delhi
is around ₹24,000). The card says "Over budget", then what the trip as asked would cost at a 4-star
hotel and the range it may land in, then three ways out as cards — a cheaper stay, a shorter trip, the
off-season — each with what it would come to and whether that fits, and two buttons: connecting flights,
and a bigger budget. Reload: all of it is still there (`GET /trips/{id}/status` → `budget_conflict.estimate`).

Pick **Stay at a budget hotel** (or the shorter trip): the plan is made on the same flights. In
`GET /trips/{id}/runs` the second `budget_decision` row says "going ahead with the cheaper stay you
chose". The off-season card moves the trip to its dates and searches the flights again for them.

---

## Step 20 — Verify Phase 22: local tips

Plan a trip in the browser (Step 16) — say Jaipur, three days, about six weeks out. Under the day
cards, above the map, is **Local tips**: five sections — Getting around, Local customs, Tourist traps,
Best times to visit, Staying safe — every one closed. Click a heading: it opens; click again: it
closes; several can be open at once. Under "Best times to visit", a place that is one of the plan's
stops is marked "In your plan · Day 1 · Evening". The line under the heading says where this comes
from: the model's own knowledge of the place, not a live source.

**The fourth agent, in the run log** (`$TRIP_ID`, `$TOKEN` as before):

```bash
curl -s "http://localhost:8000/trips/$TRIP_ID/runs" -H "Authorization: Bearer $TOKEN" | python -c "
import json, sys
from datetime import datetime, timedelta
for run in json.load(sys.stdin):
    if run['agent_name'] in ('flight_agent', 'hotel_agent', 'activities_agent', 'destination_intelligence'):
        end = datetime.fromisoformat(run['created_at'])
        start = end - timedelta(milliseconds=run['duration_ms'])
        print(f\"{run['agent_name']:26} {start:%H:%M:%S.%f} → {end:%H:%M:%S.%f}  {run['status']}\")"
```

`destination_intelligence` starts with `hotel_agent` and `activities_agent` — within a few
milliseconds — and ends inside their span: the three ran side by side. Its row's `input` is the trip
(destination, month, days, travellers, interests) and the model; its `output` has `"tool_calls": 0`
and the tips, which are the itinerary's `structured_data.local_intelligence`:

```bash
curl -s "http://localhost:8000/trips/$TRIP_ID/itinerary" -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; print(json.dumps(json.load(sys.stdin)['structured_data']['local_intelligence'], indent=1, ensure_ascii=False))"
```

Ask for a change ("a cheaper hotel"): the tips stay, and `runs` shows no second
`destination_intelligence` row. **Download PDF**: a "Local tips" page follows the days.

---

## Known first-run issues (already fixed in this repo's `requirements.txt`)

If you're on an older clone and hit these, here's what they mean and the fix:

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'email_validator'` on backend startup | `pydantic`'s `EmailStr` (used in `UserCreate`) needs this as a separate optional package | `pip install email-validator` (now pinned in `requirements.txt`) |
| `ValueError: password cannot be longer than 72 bytes` during registration/login, even with short passwords | `passlib` can't read version info from `bcrypt>=4.1`, misfires this unrelated error | `pip install "bcrypt==4.0.1"` (now pinned in `requirements.txt`) |
| `zsh: command not found: docker` | Docker Desktop not installed | See Step 0 above |
| `zsh: command not found: uvicorn` after activating venv | Either venv isn't actually active in that terminal tab, or `pip install -r requirements.txt` was never run in it | `which uvicorn` to check; re-run `pip install -r requirements.txt` if empty |
| `psql: command not found` | No local Postgres client installed | Use `docker exec -it tripplanner_postgres psql -U tripplanner -d tripplanner_db -c "..."` instead — no local install needed |

---

## Quick reference — done criteria per phase

| Phase | How to break it on purpose and explain why |
|---|---|
| **1** | Stop Docker Postgres → `GET /ping` returns `200 {"postgres":"error: ...","redis":"ok"}`. Stop Redis → `200 {"postgres":"ok","redis":"error: ..."}`. Start both → `200 {"postgres":"ok","redis":"ok"}`. Status is always 200 — errors are in the body. |
| **2** | Call `search_flights` with a past date → `ToolError PAST_DATE`. Budget < ₹2000 → `BUDGET_TOO_LOW`. Both are validated before any network call. |
| **3** | Call any tool with keys missing → `API_NOT_CONFIGURED`, server alive. Call `get_weather` with a date 30 days out → climate estimate (OWM only has 5-day window). Call twice → second call shows `[CACHE HIT]` in logs. |
| **4** | Run `alembic downgrade base` → all tables dropped. Run `alembic upgrade head` → all tables recreated. The `vector` column in `embeddings` is a pgvector type — `\d embeddings` in psql confirms it. `alembic check` reports no drift between models and migrations. |
| **5** | Omit the JWT → `401`. Use an expired/tampered JWT → `401`. Call `POST /trips` with `end_date` before `start_date` → `422`. Call `GET /trips/{id}/similar` → `501`. Publish a Redis message → it appears in the SSE stream within milliseconds. `GET /trips` → `200 []` before any trips exist, then the list after creating one. |
| **6 (Dev A)** | Mock `call_tool` to return a `ToolError` → `search_flights_node` sets `state["error"]` and `state["flights"] == []`, never raises. Omit `passengers` from input → defaults to `1`. |
| **6 (Dev B)** | Kill the MCP server subprocess mid-call → `call_tool` returns `ToolError(code="MCP_CLIENT_ERROR")`, never raises. A tool that returns a list comes back as the whole list, not its first item. `log_agent_run` writes a row with non-null `duration_ms` for every run, success or failure. |
| **7 (Router)** | Pass state with `destination=None` → `router()` returns `"clarify"`, not `"search"`. Pass state with all fields → returns `"search"`. The router is a pure Python function — zero LLM calls, fully deterministic. |
| **7 (Clarify API)** | `POST /plan` on a trip with no interests and no `raw_input` → `{"status": "clarification_needed", "question": ...}`. `POST /clarify` with `{"answer": "history and beaches"}` → `planning_started`. Send state with `{"date": None}` through retry → `not state.get(field)` correctly refills it (the old `field not in state` bug would loop forever). |
| **8** | Run HotelAgent with no `budget_per_night` → it asks a question instead of searching. Inside the orchestrator the same situation is logged as a failed search (`MISSING_INPUT`), never as a silent empty success. |
| **9** | Remove `DUFFEL_ACCESS_TOKEN` (or `LITEAPI_API_KEY`) → that badge fails on the stream, the other agents still run, the trip completes without that data. Missing interests → `["sightseeing"]`. |
| **10** | Budget ₹40,000, flights ₹28,000 → `escalate`: `budget_conflict` arrives before `planning_failed`. `POST /replan {"choice":"cheaper_flights"}` searches with a 65% cap and one more stop. `reduce_days` on a 2-day trip → `422`. After the conflict, `GET /trips/{id}/status` carries `budget_conflict` with the same reason and options, so a reloaded page can still offer them. |
| **11** | Make the builder quote a hotel at the wrong price → `budget_mismatch` (the total is recomputed from the hotel search results). A plan far *below* the user's budget passes. Retries stop at 3 and the trip fails with the evaluator's reasons. |
| **12** | Force `db.commit` to raise inside `persist_node` → neither the itinerary row nor the status change is written. |
| **13** | Raise inside the graph → trip is `failed`, `planning_failed` is published, nothing stays `planning`. Kill the server mid-run and restart → the stuck trip is marked `failed` at startup (`completed` if it was a refinement — the earlier itinerary still stands). |
| **14** | Unset `GOOGLE_API_KEY` → `planning_complete` is not delayed, the trip is `completed`, one `pending_retry` row exists, `GET /admin/embedding-health` says `degraded`; restart → it is retried. |
| **15** | Refine twice → turns 2 and 3, each on the previous turn's state. A refinement that fails leaves the trip `completed` with the earlier itinerary. `POST /refine` before any successful plan → `409`. |
| **16** | `PUT /users/preferences` with `"preferred_airlines": ["IndiGo"]` → `422` (IATA codes only). The extractor never overwrites a preference you set. |
| **Scope** | Create a trip to "London" and plan it → it fails within seconds: "London is in United Kingdom. This planner covers trips within India for now." Nothing is saved, and the reason is still there after a reload (`GET /status` → `failure_reason`). Try to create a 30-night trip → refused ("at most 14 nights"). Empty `GOOGLE_API_KEY` (or exhaust Gemini's 20 requests a day) → planning and refinements still work: the log says "Gemini unavailable … asking Groq". |
| **18 (Map)** | Open a planned trip → the map under the day cards shows numbered pins coloured by day, a line joining each day's stops, a gold hotel pin and airport markers; click a pin for its details. Null a slot's `lat` in the itinerary JSON → that pin disappears, the place is listed under the map and its day card says "No map location" — the map still renders. `GET /trips/{id}/runs` → the `persist` row's `unmapped_activities` lists it. |
| **19 (PDF)** | Start the backend with `MAP_TILE_URL=https://tiles.unreachable.invalid/{z}/{x}/{y}.png` and export a trip → still `200` and a PDF, one page shorter; `x-itinerary-map: unavailable`; the log says "the map was left out — the tile server could not be reached"; in the browser a note says the PDF came without the map. Set `MAP_TILE_URL=` (empty) → no map and no warning (`none`). Stop Redis → the export still works, it just fetches the tiles every time. In the browser's dev tools, block the request to `/export/pdf` → the toast "PDF generation failed — try again", and the button works again. |
| **17 (Frontend & SSE)** | Reload a planned trip → itinerary still shown. Stop the backend mid-plan → the panel shows "Reconnecting…" with the last known state; restart → the interrupted trip is marked `failed` and the page reports it through `GET /status`. Timestamps end in `Z`; `OPTIONS /trips` from `http://localhost:3000` is allowed, from any other origin it is not. `npx playwright test` → 34 passed. |
| **20 (Polish)** | Reload the page while the itinerary is being written → no draft (a page that joins late waits for the next build's `seq` 0), and the checked plan still arrives. A streamed reply that fails its checks is never saved — `test_a_streamed_reply_that_is_not_valid_is_never_saved` makes every reply invalid. Delete the trip's saved state (`docker exec tripplanner_redis redis-cli DEL trip:$TRIP_ID:planning_state`) and `POST /retry {"agent": "hotel_agent"}` → `409` "This plan is too old to retry a single search". Retry a search while a run is in flight → `409`. At 375 px open the progress sheet → "Ask for a change" hides until it is closed. Uninstall `uharfbuzz` (`pip uninstall uharfbuzz`) and export a Devanagari trip → the letters print unjoined, and `pytest tests/unit/test_phase20_scripts.py` fails on "HarfBuzz is installed". |
| **21 (Budget)** | `estimate_budget(…, destination="Goa", month=7)` → `season='off-peak'`, a ±10% range and no off-season price: July is the off-season. A conflict's alternatives are worked out without searching: in the run's `agent_runs`, no `hotel_agent` or `activities_agent` row comes before `escalate`. `POST /replan {"choice": "off_peak"}` on a conflict that did not offer it → `409`. Pick the shorter trip → the second `budget_decision` row is `continue`, "going ahead with the shorter trip you chose", though the flights are the same share of the budget. `pytest tests/unit/test_phase21_budget.py -k misquoted` → a ₹4,000 quote for a ₹4,500 hotel is caught, though the total is inside a peak season's ±20%. |
| **22 (Local tips)** | `cd src/frontend && npx playwright test e2e/tips.spec.ts --headed`: for "Pondicherry" the stand-in model never answers → the plan is complete, there is no Local tips section and no error anywhere on the page; for "Gokarna" it answers the second time → no tips with the plan, then they arrive with the first change and the assistant says "Local tips added". `pytest tests/unit/test_phase22_intelligence.py -k whatever_goes_wrong -v` → a rate limit, a timeout, a reply that is not JSON, a place the model does not know: each is no tips and a code, never an exception. In `GET /trips/{id}/runs` of a plan made that way, only the `destination_intelligence` row is `failed`; the trip is `completed`. `-k cannot_write` → a `local_intelligence` the plan's own model writes into its reply is dropped. |
