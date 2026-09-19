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
| 17 | `trips.py` (+ `GET /trips/{id}/status`) | Next.js 14 frontend in `src/frontend/`, `tests/unit/test_phase17_backend.py`, `tests/e2e/planning.spec.ts` |


The Phase 1 core — `health.py`, `session.py`, `redis.py`, `config.py` — was **zero-touch**.
The notable rewrites: `tools.py` (mocks → real APIs) and `main.py` (added two routers).

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

Everything else can stay as the defaults for local dev.
API keys for Phase 3 tools can be added later — without them, tools return a structured
`ToolError` instead of crashing.

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
INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema
```

Verify the tables exist (via the container — no local `psql` install needed):

```bash
docker exec -it tripplanner_postgres psql -U tripplanner -d tripplanner_db -c "\dt"
```

You should see: `agent_runs`, `embeddings`, `itineraries`, `trips`, `users`, and `alembic_version` (Alembic's own bookkeeping table — not one you created).

---

## Step 5 — Run the backend

```bash
cd src/backend
uvicorn app.main:app --reload
```

Leave this running in its own terminal tab. Do the remaining steps in a **second** tab (with `.venv` activated there too).

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
    "start_date": "2025-12-10",
    "end_date": "2025-12-17",
    "budget": 50000,
    "interests": ["beach", "food"]
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "Trip ID: $TRIP_ID"
```

Expected: `201` with the full trip object, `status: "pending"`.

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

Terminal 1 should show the event within milliseconds.

---

## Step 10 — Run all unit and contract tests ✅ Phases 1–7 check

Run from the **project root**:

```bash
pytest tests/unit/ tests/contract/ -v
```

Expected: **70 passed** — includes FlightAgent tests (6), router tests (8), clarification API tests (5), and all previous tests.

Integration tests (need Docker running):

```bash
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

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

Expected: 5 passed — structured plan, ambiguous plan, clarify completes, multi-round clarify, auth required.

### Manual verification (requires backend running + Docker)

```bash
# Create a trip and try the clarification flow
TRIP_ID=$(curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2026-12-10","end_date":"2026-12-17","budget":50000}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Plan with ambiguous input → may return clarification_needed
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/plan" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"raw_input": "I want to go somewhere warm"}' | python3 -m json.tool

# If clarification_needed, answer it:
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/clarify" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"answer": "Goa, December 15, budget 30000"}' | python3 -m json.tool
```

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
| **4** | Run `alembic downgrade -1` → all tables dropped. Run `alembic upgrade head` → all tables recreated. The `vector` column in `embeddings` is a pgvector type — `\d embeddings` in psql confirms it. |
| **5** | Omit the JWT → `401`. Use an expired/tampered JWT → `401`. Call `POST /trips` with `end_date` before `start_date` → `422`. Call `GET /trips/{id}/similar` → `501`. Publish a Redis message → it appears in the SSE stream within milliseconds. `GET /trips` → `200 []` before any trips exist, then the list after creating one. |
| **6 (Dev A)** | Mock `call_tool` to return a `ToolError` → `search_flights_node` sets `state["error"]` and `state["flights"] == []`, never raises. Omit `passengers` from input → defaults to `1`. |
| **6 (Dev B)** | Kill the MCP server subprocess mid-call → `call_tool` returns `ToolError(code="CONNECTION_REFUSED")`, never raises. `log_agent_run` writes a row with non-null `duration_ms` for every run, success or failure. |
| **7 (Router)** | Pass state with `destination=None` → `router()` returns `"clarify"`, not `"search"`. Pass state with all fields → returns `"search"`. The router is a pure Python function — zero LLM calls, fully deterministic. |
| **7 (Clarify API)** | `POST /plan` with `{"raw_input": "somewhere warm"}` → `{"status": "clarification_needed"}`. `POST /clarify` with `{"answer": "Goa, Dec 15, 30k"}` → fields filled → `planning_started`. Send state with `{"date": None}` through retry → `not state.get(field)` correctly refills it (the old `field not in state` bug would loop forever). |
| **17 (Frontend & SSE)** | Run `npm run dev` in `src/frontend/` → register/login → create trip → trigger planning in chat → observe SSE agent update pills changing in `AgentProgressPanel` in real time → itinerary day cards and cost pills render upon `planning_complete`. Drop connection → `useSSE` reconnects with exponential backoff preserving state. Run `pytest tests/unit/test_phase17_backend.py` (6 passed). Run Playwright smoke test: `npx playwright test`. |