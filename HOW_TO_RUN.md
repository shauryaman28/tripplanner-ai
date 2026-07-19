# How to Run & Verify — Phases 1–5

## What changed vs the original codebase?

| Phase | Existing files touched | New files added |
|---|---|---|
| 1 | 0 (untouched) | 0 |
| 2 | 0 (untouched) | 0 |
| 3 | `tools.py` replaced, `models.py` +2 fields, `pytest.ini` +1 line | `config.py`, `cache.py` |
| 4 | `models/__init__.py` filled in | 5 model files, full Alembic setup |
| 5 | `main.py` +2 routers | `security.py`, `deps.py`, `auth.py`, `trips.py`, 4 schema files |

The Phase 1 core — `health.py`, `session.py`, `redis.py`, `config.py` — was **zero-touch**.
The notable rewrites: `tools.py` (mocks → real APIs) and `main.py` (added two routers).

---

## Step 1 — Setup

```bash
unzip tripplanner-ai-phases1-5.zip
cd tripplanner-ai

cp .env.example .env
```

Open `.env` and set at minimum:

```env
JWT_SECRET=any-random-string-at-least-32-chars
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

Verify the tables exist:

```bash
psql postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db \
  -c "\dt"
```

You should see: `agent_runs`, `embeddings`, `itineraries`, `trips`, `users`.

Verify the pgvector column:

```bash
psql postgresql://tripplanner:tripplanner_secret@localhost:5432/tripplanner_db \
  -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='embeddings';"
```

The `vector` column should show `data_type = USER-DEFINED` (pgvector type).

---

## Step 5 — Run the backend

```bash
cd src/backend
uvicorn app.main:app --reload
```

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

Validation check — end before start should return `422`:

```bash
curl -s -X POST http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"destination":"Goa","start_date":"2025-12-17","end_date":"2025-12-10","budget":50000}' \
  -o /dev/null -w "Status: %{http_code}\n"
# → 422
```

### List trips again (now has one)

```bash
curl -s http://localhost:8000/trips \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
# → 200 [ { "id": "...", "destination": "Goa", ... } ]
```

### Trigger planning (creates an AgentRun row)

```bash
curl -s -X POST "http://localhost:8000/trips/$TRIP_ID/plan" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Expected: `202 { "status": "planning_started", "trip_id": "..." }`

### Inspect agent runs (the debugging endpoint)

```bash
curl -s "http://localhost:8000/trips/$TRIP_ID/runs" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool
```

Expected: a list with one entry — `agent_name: "orchestrator"`, `status: "pending"`.
This row was created by `POST /plan`. From Phase 9 onwards, every agent decision lands here.

### Similarity search placeholder

```bash
curl -s "http://localhost:8000/trips/$TRIP_ID/similar" \
  -H "Authorization: Bearer $TOKEN" \
  -o /dev/null -w "Status: %{http_code}\n"
# → 501  (wired in Phase 23)
```

### Itinerary (404 until Phase 12 writes one)

```bash
curl -s "http://localhost:8000/trips/$TRIP_ID/itinerary" \
  -H "Authorization: Bearer $TOKEN" \
  -o /dev/null -w "Status: %{http_code}\n"
# → 404
```

---

## Step 9 — Verify Phase 5: SSE stream

Open **two terminals**.

**Terminal 1 — subscribe to the stream:**

```bash
curl -N "http://localhost:8000/trips/$TRIP_ID/stream?token=$TOKEN"
```

You should immediately see:

```
event: connected
data: {"trip_id": "...", "status": "listening"}
```

**Terminal 2 — publish a fake agent event:**

```bash
docker exec tripplanner_redis redis-cli \
  PUBLISH "trip:$TRIP_ID:events" \
  '{"agent":"flight_agent","status":"completed","summary":"Found 3 flights from DEL to GOI"}'
```

Terminal 1 should show within milliseconds:

```
event: agent_update
data: {"agent":"flight_agent","status":"completed","summary":"Found 3 flights from DEL to GOI"}
```

This is the full SSE pipeline working — Phase 9 agents will publish to the same channel.

---

## Step 10 — Run all unit and contract tests ✅ Phases 1–3 check

Run from the **project root**:

```bash
pytest tests/unit/ tests/contract/ -v
```

All tests run with **zero network calls** — everything is mocked.

| Test file | Phase | Tests |
|---|---|---|
| `tests/unit/test_health.py` | 1 | 4 |
| `tests/unit/mcp/test_tools.py` | 3 | 17 |
| `tests/contract/test_mcp_contracts.py` | 3 | 8 |
| `tests/unit/test_auth.py` | 5 | 5 |
| `tests/unit/test_trips.py` | 5 | 6 |

Expected: **all 40 pass**.

Integration tests (need Docker running):

```bash
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

---

## Step 11 — Verify Phase 3: MCP tools

### Without API keys (safe to run now)

```bash
python -m src.ai.mcp_server.server
```

In another terminal, call a tool directly:

```bash
python3 -c "
from src.ai.mcp_server.tools import search_flights
from src.ai.mcp_server.models import FlightSearchInput
from datetime import date, timedelta

result = search_flights(FlightSearchInput(
    origin='DEL', destination='GOI',
    date=(date.today() + timedelta(days=30)).isoformat(),
    budget=20000, passengers=1
))
print(result)
"
```

Without keys you get: `error='Amadeus API not configured...' code='API_NOT_CONFIGURED'`

The server stays alive — it does not crash.

### With API keys

Add to `.env`:

```env
AMADEUS_CLIENT_ID=your_client_id
AMADEUS_CLIENT_SECRET=your_client_secret
GOOGLE_MAPS_API_KEY=your_key
OPENWEATHER_API_KEY=your_key
```

Then use **MCP Inspector** to call all 5 tools interactively:

```bash
npx @modelcontextprotocol/inspector python -m src.ai.mcp_server.server
```

### Verify Redis caching

Call a tool twice with the same params and watch the server logs:

```
INFO [CACHE MISS] mcp:flights:abc123ef   ← first call, hit the API
INFO [CACHE HIT]  mcp:flights:abc123ef   ← second call, served from Redis
```

---

## Step 12 — Swagger UI (easiest full check)

With the backend running, open **http://localhost:8000/docs**

1. Click **Authorize** → enter `Bearer <your_token>`
2. Try every endpoint from the browser UI
3. The OpenAPI schema shows all request/response shapes

---

## Quick reference — done criteria per phase

| Phase | How to break it on purpose and explain why |
|---|---|
| **1** | Stop Docker Postgres → `GET /ping` returns `200 {"postgres":"error: ...","redis":"ok"}`. Stop Redis → `200 {"postgres":"ok","redis":"error: ..."}`. Start both → `200 {"postgres":"ok","redis":"ok"}`. Status is always 200 — errors are in the body. |
| **2** | Call `search_flights` with a past date → `ToolError PAST_DATE`. Budget < ₹2000 → `BUDGET_TOO_LOW`. Both are validated before any network call. |
| **3** | Call any tool with keys missing → `API_NOT_CONFIGURED`, server alive. Call `get_weather` with a date 30 days out → climate estimate (OWM only has 5-day window). Call twice → second call shows `[CACHE HIT]` in logs. |
| **4** | Run `alembic downgrade -1` → all tables dropped. Run `alembic upgrade head` → all tables recreated. The `vector` column in `embeddings` is a pgvector type — `\d embeddings` in psql confirms it. |
| **5** | Omit the JWT → `401`. Use an expired/tampered JWT → `401`. Call `POST /trips` with `end_date` before `start_date` → `422`. Call `GET /trips/{id}/similar` → `501`. Publish a Redis message → it appears in the SSE stream within milliseconds. `GET /trips` → `200 []` before any trips exist, then the list after creating one. |
