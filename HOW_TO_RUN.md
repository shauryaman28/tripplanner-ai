# How to Run & Verify — Phases 1–17

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

Or run it in Docker (migrations run on start): `docker compose up backend`.

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

## Step 10 — Run all unit and contract tests ✅ Phases 1–17 check

Run from the **project root**:

```bash
pytest tests/unit/ tests/contract/ -v
```

Expected: **288 passed**, no network, no Docker.

Integration tests (need Docker Postgres + Redis running):

```bash
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

Expected: **14 passed**. They run against a separate `tripplanner_db_test` database
(created automatically, migrated with Alembic), so they never touch your dev data.
`test_pipeline_integration.py` is the one to watch: it drives plan → refine → add-day,
budget conflict → replan, and a no-provider run through the HTTP API with real
Postgres, Redis and the real graph — only the external APIs are stubbed.

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
It is the real app, database, Redis and graph; only the external APIs are faked:

```bash
python -m tests.e2e.stub_backend      # from the project root, serves :8000
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

Register → **Plan a trip** → fill the form → on the trip page type a request. The progress panel
turns live as agents finish, the day cards appear on `planning_complete`, and the input switches to
"Refine your trip…". Reload the page: the itinerary is still there.

Automated (starts the stub backend and the dev server itself if they are not already running):

```bash
cd src/frontend
npx playwright install chromium     # once
npx playwright test                 # 3 passed
```

Playwright runs `python -m tests.e2e.stub_backend`, so the venv must be active (or set
`PYTHON=/path/to/.venv/bin/python`). To run the same spec against real API keys, start the real
backend on :8000 first — Playwright reuses whatever is already listening.

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
| **10** | Budget ₹40,000, flights ₹28,000 → `escalate`: `budget_conflict` arrives before `planning_failed`. `POST /replan {"choice":"cheaper_flights"}` searches with a 65% cap and one more stop. `reduce_days` on a 2-day trip → `422`. |
| **11** | Make the builder quote a hotel at the wrong price → `budget_mismatch` (the total is recomputed from the hotel search results). A plan far *below* the user's budget passes. Retries stop at 3 and the trip fails with the evaluator's reasons. |
| **12** | Force `db.commit` to raise inside `persist_node` → neither the itinerary row nor the status change is written. |
| **13** | Raise inside the graph → trip is `failed`, `planning_failed` is published, nothing stays `planning`. Kill the server mid-run and restart → the stuck trip is marked `failed` at startup. |
| **14** | Unset `GOOGLE_API_KEY` → `planning_complete` is not delayed, the trip is `completed`, one `pending_retry` row exists, `GET /admin/embedding-health` says `degraded`; restart → it is retried. |
| **15** | Refine twice → turns 2 and 3, each on the previous turn's state. A refinement that fails leaves the trip `completed` with the earlier itinerary. `POST /refine` before any successful plan → `409`. |
| **16** | `PUT /users/preferences` with `"preferred_airlines": ["IndiGo"]` → `422` (IATA codes only). The extractor never overwrites a preference you set. |
| **17 (Frontend & SSE)** | Reload a planned trip → itinerary still shown. Stop the backend mid-plan → the panel shows "Reconnecting…" with the last known state; restart → the interrupted trip is marked `failed` and the page reports it through `GET /status`. Timestamps end in `Z`; `OPTIONS /trips` from `http://localhost:3000` is allowed, from any other origin it is not. `npx playwright test` → 3 passed. |
