# How to Run & Verify — Phases 1–36

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
| 23 | `trips.py` (`GET /trips/{id}/similar` — was 501 — and `GET /trips/search`), `embedder.py` (the summary text, `kind`, task types), `models/embedding.py`, `main.py` (recovery one itinerary at a time), `schemas/trip.py`, `ItineraryView.tsx`, the trips list page, `lib/api.ts`, `lib/types.ts`, `tests/fakes.py`, `tests/conftest.py`, `tests/e2e/stub_backend.py` — see `docs/phase23_build_log.md` | `src/backend/app/search.py`, `migrations/versions/004_embedding_kind.py`, `scripts/embedding_experiment.py`, `scripts/seed_demo_trips.py`, `SimilarTrips.tsx`, `tests/unit/test_phase23_similarity.py`, `tests/integration/test_phase23_similarity_integration.py`, `e2e/similar.spec.ts` |
| 24 | `tools.py` (every request through `send()`; flights and hotels cached by what the provider is asked; one search per cache key at a time; `RATE_LIMITED`), `cache.py` (`single_flight`), `server.py` (the tools run in threads), `orchestrator.py` + the three search agents (their search parameters as functions), `failures.py`, `trips.py` (`POST /trips` starts cache warming), `config.py` (`CACHE_WARMING_ENABLED`), `main.py` (the app's INFO log lines are shown), `tests/conftest.py`, `tests/fakes.py`, `tests/e2e/stub_backend.py`, `polish.spec.ts`, `.env.example` — see `docs/phase24_build_log.md` | `src/ai/mcp_server/rate_limiter.py`, `src/ai/mcp_server/outbound.py`, `src/ai/orchestrator/warming.py`, `scripts/cache_ttls.py`, `tests/unit/test_phase24_rate_limits.py`, `tests/unit/test_phase24_caching.py`, `tests/integration/test_phase24_caching_integration.py` |
| 25 | `tools.py` (every attraction says which interest it was found under; lesser-known places fill up a thin interest; "spa" searched as nothing; `estimate_budget` per person), `models.py`, `activities_agent.py` (a group searched member by member), `builder.py` (the group in the prompt, the plan repaired, the shares), `evaluator.py` (`unbalanced_group`), `orchestrator.py`, `warming.py`, `pricing.py`, `itinerary.py`, `models/trip.py` + `schemas/trip.py` + `trips.py` (`group_members`), `pdf/plan.py` + `document.py` + `formatting.py`, `main.py`, the new-trip form, `CostSummary.tsx`, `DayCard.tsx`, `ItineraryView.tsx`, `lib/types.ts` + `api.ts` + `format.ts`, `tests/fakes.py`, `tests/e2e/stub_backend.py` — see `docs/phase25_build_log.md` | `src/ai/group.py`, `migrations/versions/005_trip_group_members.py`, `GroupPanel.tsx`, `lib/group.ts`, `prompts/itinerary_builder_v9.md`, `scripts/group_experiment.py`, `tests/unit/test_phase25_group.py`, `tests/integration/test_phase25_group_integration.py`, `src/frontend/e2e/group.spec.ts` |
| 26 | `run_logger.py` (`timed_run` meters what the models are asked; a row carries `token_count` and `cost_usd`; the over-cost alert), `orchestrator.py` (a run tagged with its trip and user; "re-plan N" and "evaluator retry N" spans; a change to a plan as a trace), `mcp_client/client.py` (a span per tool call), the three search agents, `builder.py`, `destination_intelligence.py`, `preference_extractor.py`, `trips.py` (the refinement classifier timed, traced and logged), `admin.py` (`GET /admin/cost-summary`), `config.py`, `main.py`, `models/agent_run.py`, `schemas/agent_run.py`, `.env.example`, `lib/types.ts`, `tests/fakes.py`, `tests/conftest.py` — see `docs/phase26_build_log.md` | `src/ai/tracing.py`, `src/ai/usage.py`, `src/ai/trace_tree.py`, `scripts/trace_receiver.py`, `schemas/cost.py`, migration 006, `tests/unit/test_phase26_tracing.py`, `test_phase26_costs.py`, `test_version.py`, `tests/integration/test_phase26_tracing_integration.py` |
| 27 | `Dockerfile` (listens on `$PORT`, `exec`s the server, pinned installs, not root), `main.py` (the pre-start check, Sentry, recovery that leaves another instance's runs alone, waiting for plans at shutdown, `no-store`, a JSON 500), `trips.py` (a run's note kept up; a run cut short tidied up; the event stream's wait), `session.py` + `migrations/env.py` (a hosted Postgres's URL), `db/redis.py` + `mcp_server/cache.py` (retries, health checks, no password in a log), `health.py` (`GET /ready`), `admin.py` (`GET /admin/sentry-test`), `config.py`, `conversation.py`, `requirements.txt` (`sentry-sdk`), `ci.yml`, `next.config.mjs`, `lib/sse.ts`, `.env.example`, `.gitignore`, `tests/conftest.py` — see `docs/phase27_build_log.md` | `app/preflight.py`, `app/db/url.py`, `app/core/error_reporting.py`, `constraints.txt`, `railway.json`, `docker/prodlike/` (the production-like stack), `scripts/prodlike.py`, `scripts/prod_check.py`, `scripts/sentry_receiver.py`, `scripts/freeze_constraints.py`, `playwright.production.config.ts`, `e2e-production/smoke.spec.ts`, three unit test files and one integration test file |
| 28 | `.github/workflows/ci.yml` (two jobs, pushes only, the migrations up / checked / down / up, a deploy on `main` after both), `health.py` (`GET /version`), `config.py` (`GIT_COMMIT_SHA`, `settings.commit`), `error_reporting.py` (a report's release from the same commit), `next.config.mjs` (the build told its commit), `docker/prodlike/compose.yml`, `.env.example` — see `docs/phase28_build_log.md` | `.github/workflows/deploy.yml`, `.github/workflows/nightly.yml`, `.github/nightly-issue.md`, `scripts/wait_for_release.py`, `scripts/nightly_summary.py`, `scripts/nightly_post.sh`, `src/frontend/src/app/version/route.ts`, `tests/ci/fake_gh.sh`, `tests/unit/test_phase28_pipeline.py` |
| 29 | `main.py` (logging set up as the module loads, the `RequestContext` middleware, `GET /metrics`, tracing), `deps.py` (`may_read_metrics`), `trips.py` (a planning run timed, counted and one span), `session.py` / `redis.py` (statements and commands as spans), `preflight.py`, `config.py` (4 settings), `error_reporting.py` (a tracing backend's key is a secret too), `usage.py` + `run_logger.py` + `llm.py` (model calls counted per agent, every agent a span), `orchestrator.py` (builds counted), `mcp_client/client.py` + `mcp_server/server.py` + `cache.py` + `outbound.py` (the trace and the trip carried to the tool server, the cache's tally carried back), every agent's `timed_run("<its name>")`, `docker/prodlike/compose.yml`, `requirements.txt` + `constraints.txt` (18 new pins), `.env.example`, `tests/conftest.py`, `tests/fakes.py` — see `docs/phase29_build_log.md` | `src/telemetry/` (`context.py`, `metrics.py`, `traces.py`, `logs.py`), `docker/observability/` (compose, Prometheus, Alloy, Grafana's data sources and dashboard), `scripts/observability.py`, `scripts/observability_check.py`, `tests/unit/test_phase29_telemetry.py`, `test_phase29_wiring.py`, `test_phase29_stack.py`, `tests/integration/test_phase29_observability_integration.py` |
| 30 | `run_logger.py` (a row is written and not read back; `hand_back`), `orchestrator.py` + `trips.py` + `utils/embeddings.py` + `embedder.py` (a read's transaction is ended before a model, a provider or the embedding API is waited for), `config.py` (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `REDIS_MAX_CONNECTIONS`), `session.py` + `redis.py` (the pools made as the settings say, and reported), `main.py` (the event loop's lateness watched), `telemetry/metrics.py` (three gauges of what runs out), `docker/prodlike/pgbouncer.ini` + `compose.yml` (a pooler as large as the hosted one; the pools sized), the Grafana dashboard (a row for what runs out under load), `.env.example`, `.gitignore`, `tests/integration/conftest.py` — see `docs/phase30_build_log.md` | `loadtest/k6/traveller.js` (the load), `loadtest/standins/` (what answers for the models and providers), `docker/loadtest/` (the image and the stack they run in), `scripts/loadtest.py`, `docs/load-test-report.md`, `docs/loadtest/` (every run, as measured), four unit test files and one integration test file |
| 31 | `mcp_client/client.py` (a dead tool server is replaced and the call asked again; one task owns its session), `builder.py` (a timeout and a deadline for the model that writes the itinerary; `missing`), `preference_extractor.py` (a timeout), `orchestrator.py` (a timed-out build is not built again; "nothing could be searched"; a partial plan says what it lacks), `utils/failures.py` (words for a dead search service), `routes/trips.py` (a run does not wait on Redis, has a deadline as a whole, is started whatever Redis does; a planned trip whose state is gone is re-planned, not refused), `main.py` (Redis away → 503 in words), `telemetry/metrics.py` (`mcp_tool_server_starts_total`) — see `docs/phase31_build_log.md` | `loadtest/standins/faults.py` + `fault_server.py` (which model goes wrong, and the far end that fails), `docker/chaos/` (one more container), `scripts/chaos.py` (the four exercises), `docs/chaos-report.md` + `docs/chaos/` |
| 32 | `db/errors.py` (new: "the database cannot be reached", told from a statement it refused), `main.py` (that is a 503 in words; start-up recovery that could not look, looks again), `routes/trips.py` (a traveller is told what happened, never what was raised; a run that ended without marking its trip goes on trying to), `orchestrator.py` (an itinerary is saved together with the row that says its embeddings are owed), `db/session.py` (a note on why the driver has no timeout), `docker/prodlike/pgbouncer.ini` (`server_login_retry = 5`) — see `docs/phase32_build_log.md` | `scripts/chaos.py` (exercises 5 and 6), `docker/chaos/compose.yml` (Postgres keeps its data), `loadtest/standins/faults.py` + `models.py` (the embedding API can be made to fail), `docs/chaos-data-report.md` + `docs/chaos/` |
| 33 | `mcp_server/models.py` + `tools.py` (a flight search with nothing within the budget says what the cheapest flight costs), `agents/budget_decision.py` (the ways out are priced from that fare) — see `docs/phase33_build_log.md` | `evals/` (new: the scenarios, the driver, the measurements, the grader, the report), `scripts/run_eval.py` (new), `.github/workflows/nightly.yml` (the suite every night), `tests/unit/test_phase33_eval.py` + `tests/fixtures/evals/`, `docs/evals/` |
| 34 | `core/deep_health.py` (new: Postgres, Redis, the tool server, LangSmith — each asked with a deadline), `routes/admin.py` (`GET /admin/health-deep`; the embedding backlog as one function), `deps.py` (`may_check_health`), `main.py` (the embedding queue counted every 15 s; an `OSError` may be the database away), `db/errors.py` (a name that does not resolve, a network with no route: the database is away), `preflight.py`, `telemetry/metrics.py` (`embedding_queue_depth`, `mcp_cache_window_lookups`), `docker/observability/` (`compose.yml`, `prometheus.yml`, the dashboard), `scripts/observability.py` + `observability_check.py` + `trace_receiver.py`, `.github/workflows/ci.yml` (promtool and amtool) — see `docs/phase34_build_log.md` | `docker/observability/alerts/` (the four rules and promtool's tests), `docker/observability/alertmanager.yml`, `scripts/alert_receiver.py`, `scripts/alert_drill.py`, `docs/runbook.md`, `docs/alerts/` (every drill's record), two unit test files and one integration test file |
| 35 | `core/headers.py`, `core/ratelimit.py`, `core/sessions.py`, `models/refresh_token.py` + migration 007, `schemas/text.py` (new), `routes/auth.py` (refresh, logout, the limits, a bounded form), `schemas/{trip,auth,user_preferences}.py` + `routes/{trips,admin}.py` + `deps.py` (every field bounded), `core/security.py` (PyJWT), `config.py`, `error_reporting.py` (key shapes), `preflight.py`, `main.py` (the headers, every log record redacted), `telemetry/logs.py` (LogRedactor), `mcp_server/server.py`, two prompts, both Dockerfiles (`FORWARDED_ALLOW_IPS`), the frontend's `api.ts` / `sse.ts` / `AppHeader.tsx` / `next.config.mjs`, `requirements.txt` + `constraints.txt` + `package.json` — see `docs/phase35_build_log.md` | `scripts/security_check.py`, `scripts/secret_audit.py`, `scripts/provider_keys.py` (new), two unit test files, one integration test file, `e2e/session.spec.ts` |
| 36 | `src/ai/orchestrator/orchestrator.py` (the language node first, the localise node before persist; both in a change's run; the language, a typed destination and the stops' words carried), `src/ai/builder/builder.py` (local names and descriptions on a plan in another language; English untouched), `src/ai/llm.py`, `models/user_preferences.py` + `schemas/user_preferences.py` + `routes/users.py` (`preferred_language`), `routes/trips.py` (the timeline), `pdf/plan.py` + `pdf/document.py`, the frontend's `AppHeader.tsx`, `DayCard.tsx`, `LocalTips.tsx`, `ItineraryView.tsx`, `api.ts`, `types.ts`, `requirements.txt` + `constraints.txt` (langdetect) — see `docs/phase36_build_log.md` | `src/ai/language.py`, migration 008, `scripts/language_check.py`, `prompts/itinerary_builder_v10.md` + `translator_v1.md`, `src/frontend/src/lib/language.ts` + `components/PlanLanguageMenu.tsx`, `tests/fixtures/phase36/` (the English record), one unit test file, one integration test file, `e2e/language.spec.ts`, `docs/phase36_live/` |
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
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
```

`-c constraints.txt` gives you, of what the two files ask for, exactly the versions the test suites
run on — and the deployed image runs (Phase 27). Without it `pip` takes the newest of everything, and
that has already been a different app once: on a newer `sqlmodel`, registering an account answers 500.

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
INFO  [alembic.runtime.migration] Running upgrade 003 -> 004, Embeddings told apart by kind, and made again — Phase 23 similarity search
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

## Step 10 — Run all unit and contract tests ✅ Phases 1–32 check

Run from the **project root**:

```bash
pytest tests/unit/ tests/contract/ -v
```

Expected: **1365 passed**, no network, no Docker. The Phase 19 and 20 tests build real PDFs and read them back;
the Phase 24 tests run the real MCP server against fake providers, and record every wait instead of sitting through it.

Integration tests (need Docker Postgres + Redis running):

```bash
RUN_INTEGRATION=1 pytest tests/integration/ -v
```

Expected: **79 passed**. They run against a separate `tripplanner_db_test` database
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
| 15 | `curl -s -X POST …/trips/$TRIP_ID/refine -H … -d '{"message":"closer to the beach"}'` then `…/runs?turn=2` and `…/itineraries` | `turn: 2`; turn-2 rows are `refinement_classifier` (reading the message — a row of its own since Phase 26), `hotel_agent`, `itinerary_builder`, `evaluator`, `persist`, `orchestrator` (no `flight_agent`); two itinerary versions, newest first |
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
npx playwright test                 # 60 passed
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

## Step 21 — Verify Phase 23: similar trips and search

**Migrate first** — Phase 23 adds a column and re-queues every trip's embeddings:

```bash
alembic upgrade head        # 003 → 004
```

The next time the backend starts, it makes those embeddings again by itself, one trip after another
(`GET /admin/embedding-health` → `pending_retry` counts down to 0).

**Trips to compare.** Similar trips need trips to be similar to. This gives a demo account twelve — four
beach trips, two in the mountains, three heritage, two spiritual, one nature — embedded by the real
model (it needs `GOOGLE_API_KEY`, and waits out the embedding API's rate limit if it has to):

```bash
python scripts/seed_demo_trips.py        # then sign in as demo@example.com / demo-trips-123
```

**In the browser** (backend and frontend running): open **Goa** → under the map, **Similar trips**: South
Goa, Kovalam, Havelock Island — the other beach trips, not Leh. Click one: that trip opens, with similar
trips of its own. On the trips list, type into the search box and press Enter: "forts and palaces" →
Udaipur and Jaipur; "trekking in the mountains" → Leh and Manali; "quarterly tax return" → no trips match.
Empty the box and every trip is back.

**From the API** (`$TOKEN` of the demo account — Step 7's login with its email and password):

```bash
GOA=$(curl -s localhost:8000/trips -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; print(next(t['id'] for t in json.load(sys.stdin) if t['destination'] == 'Goa'))")

curl -s "localhost:8000/trips/$GOA/similar" -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; [print(r['similarity'], r['trip']['destination'], '—', r['highlight']) for r in json.load(sys.stdin)['results']]"
# 0.9057 South Goa — Palolem Beach
# 0.8566 Kovalam — Kovalam Beach
# 0.8565 Havelock Island — Radhanagar Beach

curl -s -G "localhost:8000/trips/search" --data-urlencode "q=temples and ghats" -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; [print(r['similarity'], r['trip']['destination']) for r in json.load(sys.stdin)['results']]"
# 0.7297 Varanasi
# 0.699 Rishikesh
```

The same search again is answered from the cache in a few milliseconds: the query is embedded once a day.

Plan a trip of your own while signed in as the demo account (say Kochi, "beach, food"): about a second
after the plan appears, so does its Similar trips section — the page asks again until the new plan has
been embedded.

**The experiment** behind the summary text and the cut-offs (optional; about 230 embedding calls, which
the free tier makes it wait for — give it a cache file and it can be stopped and resumed):

```bash
EMBEDDING_EXPERIMENT_CACHE=/tmp/vectors.json python scripts/embedding_experiment.py
```

---

## Step 22 — Verify Phase 24: cache warming, rate limits, backoff

Nothing to migrate. Restart the backend (Step 5) so that it runs this code, with the provider keys in `.env`.

**Cache warming.** Create a trip — the form in the browser, or Step 8's `POST /trips` — with a destination
that is a place ("Jodhpur") and some interests, and watch the backend's terminal:

```
INFO:     [CACHE WARM] weather: Jodhpur, 2026-11-19 to 2026-11-22 — 4 result(s) ready in 0.2 s
INFO:     [CACHE WARM] hotels: Jodhpur, 2026-11-19 to 2026-11-22, 2 guest(s) — 5 result(s) ready in 1.9 s
INFO:     [CACHE WARM] flights: DEL → Jodhpur on 2026-11-19, back 2026-11-22 — 1 result(s) ready in 2.0 s
INFO:     [CACHE WARM] attractions: Jodhpur: history, food — 5 result(s) ready in 2.9 s
```

The request was answered before any of that (about 30 ms): the searches run behind it. Now plan the trip
within five minutes — that is how long a flight search is kept — and read what each step took:

```bash
curl -s localhost:8000/trips/$TRIP_ID/runs -H "Authorization: Bearer $TOKEN" \
  | python -c "import json, sys; [print(f\"{r['agent_name']:<26}{r['duration_ms']:>6} ms\") for r in json.load(sys.stdin)]"
# flight_agent                  21 ms
# hotel_agent                   79 ms
# activities_agent              77 ms      ← seconds each, without the warm-up
```

The terminal shows three `[CACHE HIT]` lines for that plan and no `[CACHE MISS]`.

**The TTLs, against the Phase 3 spec.** With something cached (the trip you just created will do):

```bash
python scripts/cache_ttls.py
# cache         keys   expires in               spec
# flights          1   280 s                    300 s          ok
# hotels           1   880 s                    900 s          ok
# attractions      2   9,750–21,581 s           21,600 s       ok
# weather          1   3,579 s                  3,600 s        ok
# geocode         14   2,346,213–2,591,980 s    2,592,000 s    ok   (not in the Phase 3 spec)
```

Or by hand, one family at a time. (`redis-cli TTL 'mcp:flights:*'` is not the way: `TTL` takes a key, not
a pattern, and answers `-2` — "no such key" — whatever is cached.)

```bash
docker exec tripplanner_redis redis-cli --scan --pattern 'mcp:flights:*' \
  | while read key; do echo "$(docker exec tripplanner_redis redis-cli ttl "$key")  $key"; done
# 280  mcp:flights:336bc3b33afcce92035fb55a580ee190
```

**The rate limit, on a real provider.** The geocoder allows one request a second. Ask for three places
nobody has looked up in the last thirty days, all at once (from the project root; no model is called):

```bash
python - <<'EOF'
import asyncio, time
from src.ai.mcp_client.client import call_tool, close_session

async def main():
    began = time.perf_counter()
    async def look(place):
        await call_tool("get_attractions", {"destination": place, "interests": ["history"], "limit": 5})
        print(f"{place:<10} answered after {time.perf_counter() - began:4.1f} s")
    await asyncio.gather(*(look(place) for place in ("Bundi", "Orchha", "Mandu")))
    await close_session()

asyncio.run(main())
EOF
# [RATE LIMIT] nominatim: limit of 1 per 1.1 s reached — waiting 1.10 s
# [RATE LIMIT] nominatim: limit of 1 per 1.1 s reached — waiting 2.20 s
# Bundi      answered after  1.9 s
# Orchha     answered after  2.3 s
# Mandu      answered after  4.3 s
```

The requests were spaced out, not turned away: there is no `[BACKOFF]` line. (Run it again and nothing
waits — the places are cached now. Pick three others.)

**Backoff, on a forced 429.** Provoking a real 429 means sending a provider more than it allows, so this
one is shown on a fake that answers 429, 429, 429, 200:

```bash
pytest tests/unit/test_phase24_rate_limits.py -k roughly_double -o log_cli=true --log-cli-level=WARNING
# [BACKOFF] duffel: HTTP 429 — waiting 1.30 s before attempt 2 of 4
# [BACKOFF] duffel: HTTP 429 — waiting 2.85 s before attempt 3 of 4
# [BACKOFF] duffel: HTTP 429 — waiting 4.45 s before attempt 4 of 4
```

Each wait is about twice the last, plus up to a second of jitter — different numbers on every run.
`-k three_agents -v` is the roadmap's scenario: three agents at once on a provider that allows two
requests a window, and not one 429.

**In the browser.** `cd src/frontend && npx playwright test e2e/polish.spec.ts --headed`: for "Munnar" the
stand-in flight provider says "asked too often" through every retry. The progress panel says "This search
is busy right now. Try again in a minute." — not "HTTP 429" — and **Retry the flight search** finds the flights.

---

## Step 23 — Verify Phase 25: a group trip

**Migrate first** — Phase 25 adds a column:

```bash
alembic upgrade head        # 004 → 005: trips.group_members
```

**In the browser** (backend and frontend running, the provider keys in `.env`): **Plan a trip** → a destination
("Goa"), four days, a budget, **Travellers: 4** → **Do they want different things? Say what each traveller
enjoys**. A row appears for each traveller. The roadmap's four:

| Name | Interests |
|---|---|
| Asha | beach, food |
| Ben | history, culture |
| Chitra | adventure |
| Dev | spa, relaxation |

**Create & plan**, then say what you want in the chat ("a relaxed few days for the four of us"). When the
plan is there:

- under the total: **₹16,834 / person · shared equally by 4 travellers** (your numbers will differ);
- **Who it is for**: each traveller, their interests and how many stops are theirs — and, under Dev,
  "Nothing was found in Goa for: spa". The provider lists no spas; the plan says so instead of pretending;
- on every stop: **For Asha and Dev**, **For Ben**…  Count them: in days 1–2 and again in days 3–4, every
  one of the four has at least one;
- **Download PDF**: the share is on the cover under the total, and "For …" is among each stop's facts.

**From the API** (`$TOKEN` from Step 7):

```bash
TRIP_ID=$(curl -s -X POST localhost:8000/trips -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "destination": "Goa", "start_date": "2026-12-10", "end_date": "2026-12-13", "budget": 120000,
  "group_members": [
    {"name": "Asha",   "interests": ["beach", "food"]},
    {"name": "Ben",    "interests": ["history", "culture"]},
    {"name": "Chitra", "interests": ["adventure"]},
    {"name": "Dev",    "interests": ["spa", "relaxation"]}]}' | python -c "import json, sys; print(json.load(sys.stdin)['id'])")
# the backend's terminal: four "[CACHE WARM] attractions: Goa: …" lines — one search per traveller

curl -s -X POST localhost:8000/trips/$TRIP_ID/plan -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"raw_input": "A relaxed few days for the four of us"}'
# …wait for GET /trips/$TRIP_ID to say "completed", then:

curl -s localhost:8000/trips/$TRIP_ID/itinerary -H "Authorization: Bearer $TOKEN" | python -c "
import json, sys
plan = json.load(sys.stdin)['structured_data']
for day in plan['days']:
    for slot in ('morning', 'afternoon', 'evening'):
        if day.get(slot): print(day['day'], slot.ljust(9), day[slot]['activity'][:36].ljust(36), 'for', ', '.join(day[slot]['suits'] or []))
print('total', plan['total_cost'], '· per person', plan['per_person_cost'])
print([(m['name'], m['stops'], m['nothing_for']) for m in plan['group']['members']], 'balanced:', plan['group']['balanced'])"
# 1 morning   Arossim Beach                        for Asha, Dev
# 1 afternoon Goa Chitra museum                    for Ben
# 1 evening   Dudhsagar Falls                      for Chitra, Dev
# …
# total 67336.74 · per person 16834.19
# [('Asha', 5, []), ('Ben', 5, []), ('Chitra', 4, []), ('Dev', 8, ['spa'])] balanced: True
```

`group_size` was not sent: four travellers were named, so it is 4. The per-person figure is the total
divided by it; `per_person_breakdown.shares` are whole rupees that add up to the total exactly.

**The search behind it**, without a model or a trip — the same four travellers against the real attraction
search in five destinations, as one traveller and one by one (needs `OPENTRIPMAP_API_KEY`):

```bash
python scripts/group_experiment.py
# == Goa, 4 days ==
#   together      9 places:  Asha 4   Ben 4   Chitra 2   Dev 2
#   one by one   Asha    found 10
#   one by one   Dev     found 10   nothing for: spa
#                12 places:  Asha 5   Ben 5   Chitra 4   Dev 8
#   a plan       days 1–2: Asha 2   Ben 2   Chitra 2   Dev 4
#   a plan       days 3–4: Asha 2   Ben 1   Chitra 1   Dev 3
#                balanced: True
```

**In the browser, on the stub stack**: `cd src/frontend && npx playwright test e2e/group.spec.ts --headed`.

---

## Step 24 — Verify Phase 26: traces, tokens and what they cost

**Migrate first** — Phase 26 adds two columns:

```bash
alembic upgrade head        # 005 → 006: agent_runs.token_count, agent_runs.cost_usd
```

**See a trace — no LangSmith account needed.** Terminal 1, from the project root: a local stand-in for
LangSmith that prints every trace it is sent.

```bash
python scripts/trace_receiver.py
# Receiving traces on http://localhost:1984 — set LANGCHAIN_ENDPOINT to that, and plan a trip.
```

Terminal 2: the backend, pointed at it for this run only. Stop the one from Step 5 first. `.env` is not
touched — and its provider keys are needed, because this is a real plan by the real models:

```bash
LANGCHAIN_TRACING_V2=true LANGCHAIN_API_KEY=local LANGCHAIN_ENDPOINT=http://localhost:1984 \
  uvicorn app.main:app --app-dir src/backend
# [TRACING] LangSmith tracing is on — project 'tripplanner-ai', sent to http://localhost:1984
```

Terminal 3 — plan a trip (`$TOKEN` from Step 7):

```bash
TRIP_ID=$(curl -s -X POST localhost:8000/trips -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"destination":"Jaipur","start_date":"2026-12-10","end_date":"2026-12-13","budget":70000,"group_size":2,"interests":["history","food"]}' \
  | python3 -c "import json, sys; print(json.load(sys.stdin)['id'])")
curl -s -X POST localhost:8000/trips/$TRIP_ID/plan -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"raw_input":"A relaxed long weekend of forts and good food"}'
```

A few seconds after it has finished, Terminal 1 has printed the run (shortened here — the two search agents
look like the flight agent):

```
plan trip   trip 45a86c91  user 8ce59c4b  turn 1                       9.1 s    6,419 tokens
├─ intent_parsing                                                      1.3 s      432 tokens
│  └─ ChatGoogleGenerativeAI  [llm]                                    1.3 s      432 tokens
├─ apply_preferences                                                   0.0 s
├─ run_flight                                                          0.0 s
│  └─ flight_agent                                                     0.0 s
│     ├─ intent_parsing                                                0.0 s
│     │  └─ router                                                     0.0 s
│     └─ search_flights                                                0.0 s
│        └─ mcp:search_flights  [tool]                                 0.0 s
├─ budget_decision                                                     0.0 s
├─ hotel_activities                                                    1.5 s      884 tokens
│  ├─ hotel_agent …  ├─ activities_agent …
│  └─ destination_intelligence                                         1.5 s      884 tokens
│     └─ ChatGroq  [llm]                                               1.4 s      884 tokens
├─ build_itinerary                                                     5.2 s    4,309 tokens
│  └─ ChatGroq  [llm]                                                  5.2 s    4,309 tokens
├─ evaluate                                                            0.0 s
├─ persist                                                             0.0 s
├─ merge                                                               0.0 s
└─ extract_preferences                                                 1.0 s      794 tokens
   └─ ChatGroq  [llm]                                                  1.0 s      794 tokens
```

One trace; a line for every step the graph took; a model call under the step that made it, with its tokens;
each step's tokens the sum of what is inside it. The trip and the user are on the first line because they are
on every span.

**Only this trip's traces** — what the filter on `trip_id` does in LangSmith:

```bash
curl -s "localhost:1984/traces?trip_id=$TRIP_ID" | python3 -c "
import json, sys
for trace in json.load(sys.stdin): print(trace['name'], '·', trace['tokens'], 'tokens · turn', trace['metadata']['turn'])"
# plan trip · 6419 tokens · turn 1
```

**The same run in the database** — the tokens each step asked for, and their list price in dollars:

```bash
curl -s localhost:8000/trips/$TRIP_ID/runs -H "Authorization: Bearer $TOKEN" | python3 -c "
import json, sys
for run in json.load(sys.stdin): print(f\"{run['agent_name']:<26}{str(run['token_count'] or '—'):>7}  {run['cost_usd'] or ''}\")"
# intent_parsing                432  0.00031
# preferences                     —
# flight_agent                    —
# budget_decision                 —
# hotel_agent                     —
# activities_agent                —
# destination_intelligence      884  0.00033015
# itinerary_builder            4309  0.00162735
# evaluator                       —
# persist                         —
# preference_extractor          794  0.00021135
# orchestrator                    —
```

Add the tokens up: they are the trace's total, to the token (6,419 here). A step that asked no model has
no tokens — not 0. Your numbers will differ; the two totals will not.

**What it all cost:**

```bash
curl -s "localhost:8000/admin/cost-summary?since=2026-10-01" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# "total": {"runs": 4, "tokens": 6419, "cost_usd": 0.002479, "unpriced_tokens": 0},
# "by_agent": [{"agent": "itinerary_builder", …}, …]      ← dearest first
# "by_date":  [{"date": "2026-10-11", …}]                  ← oldest first
```

`since=yesterday` → `422`; no token → `401`. If `unpriced_tokens` is not 0, the backend's log names the model
that has no price: add it with `LLM_PRICES` in `.env` (the format is in `.env.example`).

**A change is two more traces under the same trip:**

```bash
curl -s -X POST localhost:8000/trips/$TRIP_ID/refine -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"Could we stay somewhere nicer? A better hotel please."}'
# Terminal 1:
# classify refinement   trip 45a86c91  user 8ce59c4b  turn 2             1.2 s      524 tokens
# refine trip: targeted_hotel   trip 45a86c91  user 8ce59c4b  turn 2     6.1 s    4,712 tokens
# …and `…/runs?turn=2` starts with a row named refinement_classifier
```

**The re-planning branch.** Plan the same trip with a budget its flights only just do not fit — the run
above logged the fare (`…/runs`, the `budget_decision` row's `output.flight_cost`); a budget of 1.8 × that fare
leaves 44%, under the half that re-planning starts at. With a fare of ₹8,365, `"budget": 15000`:

```
├─ run_flight
│  └─ re-plan 1: cheaper flights                                       3.8 s
│     └─ flight_agent → search_flights → mcp:search_flights  [tool]
├─ budget_decision
├─ run_flight
│  └─ re-plan 2: cheaper flights                                       0.0 s
│     └─ … mcp:search_flights  [tool]        ✗ NO_RESULTS: No flights from DEL to JAI within ₹8,250.
├─ budget_decision
└─ escalate
```

**The alert.** A plan costs about a quarter of a cent, so to see the warning lower the limit: restart Terminal 2's
backend with `LLM_COST_ALERT_USD=0.002` in front of the same command, and plan a trip. The backend logs, once:

```
[COST ALERT] trip 528f9612-…: the models have cost $0.0025 over 3 runs — past the $0.002 one trip is expected to stay under
```

**What left the machine.** `/records` is every run exactly as it was sent. None of the keys in `.env` is in it:

```bash
curl -s localhost:1984/records > /tmp/sent.json
for key in GROQ_API_KEY GOOGLE_API_KEY DUFFEL_ACCESS_TOKEN JWT_SECRET; do
  value=$(grep "^$key=" .env | cut -d= -f2-)
  [ -n "$value" ] && echo "$key: $(grep -c -F "$value" /tmp/sent.json) occurrences in what was sent"
done
# GROQ_API_KEY: 0 occurrences in what was sent       (and so on — the loop prints counts, never a key)
rm /tmp/sent.json
```

What is in it: the destination, the dates, the budget, the interests, the prompts and the plan. That is what a
trace is — and why tracing is off unless `.env` switches it on.

**In LangSmith itself** — with `LANGCHAIN_API_KEY=<your key>` in `.env` (smith.langchain.com → Settings → API keys):
start the backend with `LANGCHAIN_TRACING_V2=true` in front of the command and no `LANGCHAIN_ENDPOINT`, and plan a
trip, then change it. The log must say `sent to LangSmith`. In LangSmith, in the project `tripplanner-ai`:

- one trace named `plan trip`, with the tree above; its token total is the sum of the trip's rows, to the token;
- filter the project's runs on the metadata key `trip_id` with the trip's id as the value → that trip's traces
  and no other's: `plan trip`, `classify refinement`, `refine trip: …` — **all in this one project**. (On the
  first run against LangSmith the plan was in a project called `default` and only the changes were here; a trace
  is now handed its project instead of being left to find one. `curl localhost:1984/traces` shows the same thing
  locally: every trace's `"project"` is the one you set.)
- LangSmith shows a cost for each model call from a price list of its own. It should agree with the row's
  `cost_usd` to the last digit; where it does not, a price has moved — look it up on the provider's page and
  correct it with `LLM_PRICES`.

This was done on 2026-10-11; what it found is in `docs/phase26_build_log.md`, "In LangSmith itself".

**The tests:** `pytest tests/unit/test_phase26_tracing.py tests/unit/test_phase26_costs.py -v`, and
`RUN_INTEGRATION=1 pytest tests/integration/test_phase26_tracing_integration.py -v`.

---

## Step 25 — Verify Phase 27: production

Two halves. **A** needs nothing but Docker and the keys already in `.env`: the image that is deployed,
on a stack that behaves the way the hosted services do. **B** is the deploy itself, and needs your own
accounts on Neon, Upstash, Sentry, Railway and Vercel — it has not been done from this repository.

### A. The production-like stack (no accounts)

Four terminals, all from the project root.

```bash
# Terminal 1 — a stand-in for Sentry: prints every error report it is sent
python scripts/sentry_receiver.py
# Receiving error reports on http://localhost:9010 — set SENTRY_DSN=http://standin@localhost:9010/1

# Terminal 2 — the stack: builds the image, starts it against a TLS-only Postgres behind a pooler
# and a TLS-only Redis, and waits until it is ready (first time: a few minutes)
python scripts/prodlike.py up
# wrote .env.prodlike (git-ignored): a signing secret and an operations token for this stack
# …
# ready: http://localhost:8200/ready answers 200.  Next: python scripts/prod_check.py --prodlike

python scripts/prodlike.py logs
# [PREFLIGHT] APP_ENV=production
# Every setting a deployment needs is there.
# INFO  [alembic.runtime.migration] Running upgrade  -> 001, Initial schema — …      ← an empty database, migrated
# INFO:     Started server process [1]                                                 ← the server is process 1
# INFO:     [STARTUP] production — database pooler:6432/tripplanner, Redis rediss://upstash-standin:6379
# INFO:     [SENTRY] error reporting is on — environment 'production', release 'tripplanner-ai@0.27.0', …
# INFO:     Uvicorn running on http://0.0.0.0:8200                                     ← the port it was told

# Terminal 3 — the frontend, built for production with the backend's address compiled in
cd src/frontend
cp tsconfig.json /tmp/tsconfig.json.keep
NEXT_DIST_DIR=.next-prodlike BACKEND_URL=http://localhost:8200 NEXT_PUBLIC_API_URL=http://localhost:8200 npx next build
cp /tmp/tsconfig.json.keep tsconfig.json      # a build into another directory adds a line to it
NEXT_DIST_DIR=.next-prodlike BACKEND_URL=http://localhost:8200 NEXT_PUBLIC_API_URL=http://localhost:8200 npx next start -p 3200
```

**The acceptance, asked of the running stack** (Terminal 4). It plans one real trip:

```bash
python scripts/prod_check.py --prodlike
# 1. The backend answers                  ✓ GET /ready is 200 · ✓ Cache-Control: no-store · ✓ this version
# 2. pgvector on the database             ✓ found, 0.8.6 · ✓ newest migration · ✓ HNSW index · ✓ encrypted
# 3. Redis pub/sub over TCP               ✓ PING with TLS · ✓ a published message arrives at a subscriber
# 4. A deliberate error reaches Sentry    ✓ 404 without the token · ✓ 500 with it · ✓ the report arrived
#                                         ✓ nothing secret in anything the stand-in was sent
# 5. The frontend answers                 ✓ page loads · ✓ /api/* reaches the backend · ✓ not cached
# 6. A trip is planned end to end         ✓ … ✓ the event stream opens, straight from the backend
#                                         ✓ CORS · ✓ POST /plan answers at once — 202 in 20 ms
#                                         ✓ the plan arrives over the stream — 9.9 s; 40 events
#                                         ✓ none of them comes near the 120 s the frontend's host allows
# 29 passed, 0 failed, 0 skipped
```

Terminal 1 has printed the error that was raised on purpose —
`✗ DeliberateError: deliberate test error check-…   production · tripplanner-ai@0.27.0 · GET /admin/sentry-test` —
and `curl -s localhost:9010/raw` is every report exactly as it was sent: search it for any key in `.env`.

**The same in a real browser** — which is what enforces the cross-origin rule on the event stream:

```bash
cd src/frontend && npx playwright test -c playwright.production.config.ts
# planned on http://localhost:3200; its event stream came from http://localhost:8200; …
#   ✓  a traveller plans a trip on the deployed site
```

**What a deploy does to a plan in flight.** Register once in the browser at http://localhost:3200, create a
trip, and then, as you send the chat message that starts the plan:

```bash
docker stop -t 70 tripplanner-prodlike-backend-1     # what a platform does: SIGTERM, then a grace period
python scripts/prodlike.py logs | grep SHUTDOWN
# [SHUTDOWN] 1 planning run(s) in flight — waiting up to 40 s for them      ← the plan is finished first
docker start tripplanner-prodlike-backend-1
```

Reload the page: the itinerary is there. Do it again with `docker kill tripplanner-prodlike-backend-1` (no
warning, as a crash) and `docker start` it straight away: the log says
`[STARTUP RECOVERY] 1 trip(s) are being planned by another instance — left alone, looked at again in 45 s`,
then `1 run(s) died with the instance that had them`, and the page says the server was restarted and to start
the plan again — instead of "planning" for ever.

**What a hosted Redis does to connections:**

```bash
REDIS="docker exec tripplanner-prodlike-upstash-standin-1 redis-cli --tls --insecure --no-auth-warning -a standin_redis_password"
$REDIS client kill type normal        # every ordinary connection closed, as when a hosted Redis redeploys
curl -s localhost:8200/ready          # {"postgres":"ok","redis":"ok"} — the command was sent again on a new one
$REDIS client kill type pubsub        # with a trip's page open: its event stream stays open, and still hears
```

```bash
python scripts/prodlike.py down       # stops it and deletes everything it made
```

### B. The deploy itself (your accounts)

**1. The values.** Put them in `.env.production` (git-ignored; never in chat, never committed), and have them
judged before any platform sees them:

```bash
PYTHONPATH=src/backend python -m app.preflight --env-file .env.production --production
# Cannot start in production:
#   ✗ REDIS_URL is an https:// address — that is a REST API, and a REST API cannot subscribe: …
# Starts, and will not fully work:
#   ! OPS_TOKEN is empty: GET /admin/sentry-test is switched off, so error reporting cannot be tried out
```

| Variable | Where it comes from |
|---|---|
| `APP_ENV` | `production` (the image's default) |
| `DATABASE_URL` | Neon → your project → Connection string → **Pooled connection**, copied as it is |
| `DATABASE_URL_UNPOOLED` | optional — the same page with pooling off; only the migrations use it |
| `REDIS_URL` | Upstash → your Redis database → the `rediss://…:6379` URL. Not the REST URL |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | optional (Phase 30) — how many database connections one instance may hold: 5 kept and 10 more by default. Together, times two (a deploy has two instances alive at once), they must fit in what the pooler lends: 90 % of Neon's `max_connections`, 94 on the smallest compute. `10` and `30` are what the load test ran on |
| `REDIS_MAX_CONNECTIONS` | optional (Phase 30) — how many connections to Redis one instance may open, default 100. Every open trip page keeps one, so this is how many pages can be open at once; past it requests fail. Set it as high as your Redis plan's limit of concurrent connections allows — the load test ran on `500` |
| `JWT_SECRET` | `openssl rand -hex 32` — a new one, not your laptop's |
| `OPS_TOKEN` | `openssl rand -hex 32` |
| `SENTRY_DSN` | Sentry → new project, platform FastAPI → Client Keys (DSN) |
| `CORS_ORIGINS` | the frontend's address, once you have it (step 4): `https://<your-app>.vercel.app` |
| `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GROQ_API_KEY`, `DUFFEL_ACCESS_TOKEN`, `LITEAPI_API_KEY`, `OPENTRIPMAP_API_KEY`, `OPENWEATHER_API_KEY` | as in your `.env` |
| `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` | optional — as in your `.env`, to trace production runs |

**2. The backend → Railway.** New Project → Deploy from GitHub repo → this repository. `railway.json` tells it
the rest: build `src/backend/Dockerfile`, check `GET /ready`, give a stopping instance 70 seconds. Under
Settings → Networking, **Generate Domain** — that address is needed before the first deploy can succeed.
Under Variables, paste the table above (Raw Editor takes the file). Leave `PORT` alone: Railway sets it.

**3. The frontend → Vercel.** Add New → Project → the same repository, **Root Directory `src/frontend`**.
Environment variables, before the first build — both the backend's Railway address:
`BACKEND_URL=https://<backend>` and `NEXT_PUBLIC_API_URL=https://<backend>`. (Without them the build stops and
says so. They are compiled in: change them and you must redeploy.)

**4. Close the loop.** Put the Vercel address in Railway's `CORS_ORIGINS` (no trailing slash). Railway
redeploys. The order is forced: without a deployed origin there the backend refuses to start, because the
browser would refuse the event stream.

**5. Ask it.**

```bash
python scripts/prod_check.py --api https://<backend> --web https://<frontend> --env-file .env.production
cd src/frontend && E2E_PRODUCTION_URL=https://<frontend> npx playwright test -c playwright.production.config.ts
```

Check 4 prints what to search Sentry for (`check-…`): the roadmap's "one deliberate test error appears in
Sentry". With `SENTRY_AUTH_TOKEN`, `SENTRY_ORG` and `SENTRY_PROJECT` in the env file it is read back for you.
Each run leaves one account (`prod-check-…@example.com` / `browser-check-…@example.com`) and one trip behind.

What to expect to be different from the stand-in, because no stand-in can know: `docs/phase27_build_log.md`,
"The deploy itself".

**The tests:** `pytest tests/unit/test_phase27_production.py tests/unit/test_phase27_error_reporting.py
tests/unit/test_phase27_runs.py -v`, and
`RUN_INTEGRATION=1 pytest tests/integration/test_phase27_production_integration.py -v`.

---

## Step 26 — Verify Phase 28: the pipeline

Three workflows in `.github/workflows/`. What each does can be run on your own machine, command for command;
what only GitHub can show — and the three things only you can set — are at the end.

### A. What CI runs, by hand

```bash
# the backend job — lint and tests (about 25 seconds on a laptop); its migrations step is below
ruff check . && ruff format --check .
pytest tests/unit/ tests/contract/ -v          # 1365 passed

# the frontend job
cd src/frontend && npm ci && npx tsc --noEmit && npm run lint && npm run build
```

The job's third step takes every table down and puts it up again — **never run it on a database you want to
keep**. CI does it on a database of its own; so do you, on a scratch one:

```bash
docker exec tripplanner_postgres createdb -U tripplanner tripplanner_scratch
DATABASE_URL=postgresql://tripplanner:<your password>@localhost:5432/tripplanner_scratch \
  sh -c 'alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head'
# …
# No new upgrade operations detected.
docker exec tripplanner_postgres dropdb -U tripplanner tripplanner_scratch
```

Now break it. Add a column to a model and no migration — in `src/backend/app/models/trip.py`, inside `Trip`:
`a_column_nobody_migrated: str | None = None` — and run `alembic check` again:

```
FAILED: New upgrade operations detected: [('add_column', None, 'trips', Column('a_column_nobody_migrated', …))]
```

That is the line that fails the build. Take the column out again.

### B. The same, as GitHub runs it — on your machine

[`act`](https://github.com/nektos/act) runs the workflow files themselves, in containers, with the service
containers they ask for. It needs Docker, and ports 5432 and 6379 free — stop the development database first.

```bash
brew install act
docker compose stop postgres redis
touch /tmp/empty.env    # see the note below: without this, act hands your .env to every job

act push -W .github/workflows/ci.yml -j backend  --env-file /tmp/empty.env --secret-file /tmp/empty.env --var-file /tmp/empty.env
act push -W .github/workflows/ci.yml -j frontend --env-file /tmp/empty.env --secret-file /tmp/empty.env --var-file /tmp/empty.env
act workflow_dispatch -W .github/workflows/deploy.yml --env-file /tmp/empty.env --secret-file /tmp/empty.env --var-file /tmp/empty.env
# [Deploy/backend → Railway]   ::notice title=Nothing was deployed::The secret RAILWAY_TOKEN or the variable RAILWAY_SERVICE is not set …

docker compose start postgres redis
```

**`act` reads the repository's `.env` and gives every variable in it to every job** — your real keys, and your
laptop's `DATABASE_URL` in place of the job's own. GitHub does no such thing. The three `--…-file` options
pointing at an empty file switch it off; a run without them tells you nothing about what GitHub will do.

The first run downloads a runner image of about 1.5 GB. If a run stops with "port is already allocated", a
service container from the run before is still there: `docker ps -aq --filter name=act- | xargs docker rm -f`.

### C. The scripts behind the deploy and the night

```bash
# the wait for a deploy — against the production-like stack of Step 25
PRODLIKE_COMMIT=$(git rev-parse HEAD) python scripts/prodlike.py up
curl -s localhost:8200/version                 # {"version":"0.28.0","commit":"45c070e9…"}
python scripts/wait_for_release.py --commit $(git rev-parse HEAD) --api http://localhost:8200
#   ✓ the backend is running 45c070e  (after 0 s)
#   **45c070e is live** — the backend after 0 s.

python scripts/wait_for_release.py --commit 0000000000 --api http://localhost:8200 --timeout 12 ; echo "exit $?"
#   error: the backend is not running 0000000 after 12 s — last answer: commit 45c070e92143
#   exit 1
python scripts/prodlike.py down

# the night's comment — from real reports
RUN_INTEGRATION=1 pytest tests/integration/ -q --junitxml=/tmp/reports/integration.xml
python scripts/nightly_summary.py /tmp/reports --require integration --absent evals="no eval suite yet (Phase 33)"
#   ### ✅ Nightly, 2026-10-11 — 60 passed
#   | integration | 60 | 0 | 0 | 31 s |
#   | evals | — | — | — | not run: no eval suite yet (Phase 33) |

# …and from none: a night whose tests never started is not a night with no failures
python scripts/nightly_summary.py /tmp/an-empty-directory --require integration ; echo "exit $?"
#   ### ❌ Nightly, 2026-10-11 — 1 failed
#   | integration | — | — | — | DID NOT RUN — no report was written |
#   exit 1
```

### D. On GitHub — and what is yours to set

**Every push** shows two checks, `backend` and `frontend`, on the commit and on its pull request (Actions tab
→ CI). To see one fail: push a branch with `assert 1 + 1 == 3` in a new test; the commit gets a red cross
and the run's log ends `1 failed, 1365 passed`. Delete the branch afterwards.

**1. Make a red cross block the merge.** Settings → Branches → Add branch ruleset (or "Add classic branch
protection rule") for `main` → *Require status checks to pass* → add `backend` and `frontend` → also
*Require branches to be up to date*. **On a private repository this needs GitHub Pro**; on the free plan the
option is shown and not enforced. Until then the checks inform and do not block — except the deploy, which
never runs for a commit that failed.

**2. Make a merge deploy.** After Step 25 B (the Railway service and the Vercel project exist):

| Where | Name | Value |
|---|---|---|
| Settings → Secrets and variables → Actions → **Secrets** | `RAILWAY_TOKEN` | Railway → your project → Settings → Tokens → a **project token** for the production environment |
| … → **Variables** | `RAILWAY_SERVICE` | the backend service's name in Railway |
| … → **Variables** | `PRODUCTION_API_URL` | `https://<backend>` |
| … → **Variables** | `PRODUCTION_WEB_URL` | `https://<your-app>.vercel.app` |

In Railway, switch **off** the service's own deploy-on-push (the service → Settings → **Disable** automatic
deployments): with both on, every merge deploys twice, and one of the two without waiting for the tests. Do
not use Railway's "Wait for CI" together with this pipeline either — it waits for the run to finish, and the
run's last job is waiting for Railway. In Vercel nothing is needed beyond Step 25 B: its GitHub integration
builds every push to `main` by itself. (The site learns its commit from Vercel's `VERCEL_GIT_COMMIT_SHA`:
Settings → Environment Variables → "Automatically expose System Environment Variables" must be on, which is
the default. With it off the site's `/version` answers `null` and the wait never ends.)

Then merge something. The run's page (Actions → CI → the `deploy` job) ends with
"**abc1234 is live** — … From the merge to live: **N s**". More than 300 is a warning on the run; not live
after ten minutes fails it.

**3. The night.** Nothing to set. At 02:00 UTC — 07:30 in India — the integration tests run, and a comment
appears on the issue "Nightly test results" (label `nightly`; the first night creates both). To have one now:
Actions → Nightly → Run workflow. Unsubscribe from the issue if a notification a day is too many; a bad
night also fails its run, and GitHub mails whoever last changed the workflow's schedule. GitHub starts
scheduled runs late when it is busy — the top of an hour is its busiest — and says it may drop one
altogether: a date missing from the issue is a night that never ran, not a night that passed.

**The cost.** A private repository on the free plan has 2,000 Actions minutes a month, and every job is
rounded up to a whole minute. From the local runs a push should cost 4–5 and a night 2–3 (up to 90 a month),
which leaves room for some 380 pushes a month — an estimate until GitHub has run them: Settings → Billing →
Usage has the figure.

**The tests:** `pytest tests/unit/test_phase28_pipeline.py -v` (30).

---

## Step 27 — Verify Phase 29: metrics, traces, log lines

Needs Docker and nothing else: no account, no key beyond the ones in `.env`. Three things come out of the
app — numbers at `GET /metrics`, traces, and log lines as JSON — and five programs on your machine read them.

### A. The stack

```bash
python scripts/observability.py up
# waiting for Prometheus, Grafana, Jaeger and Loki to answer…
# ready.
#   Grafana      http://localhost:3001
#   Prometheus   http://localhost:9090/targets
#   Jaeger       http://localhost:16686
```

The first time it downloads five images, about 1 GB. Nothing in it is in the app's path: with the stack
down the app runs exactly as before.

### B. Your backend, watched

`GET /metrics` is always there. The other two are settings — where traces go, and that log lines are JSON —
and the lines have to be somewhere the stack can read: `./logs`. Stop the backend of Step 5 (Ctrl-C) and
start it like this, from the project root:

```bash
mkdir -p logs
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 LOG_FORMAT=json \
  uvicorn app.main:app --app-dir src/backend --port 8000 2>&1 | tee logs/backend.log
# {"timestamp": "2026-10-12T13:20:04.118+00:00", "level": "info", "logger": "src.telemetry.traces",
#  "message": "[OTEL] tracing is on — service 'tripplanner-backend', sent to http://localhost:4318, 100% of requests",
#  "service": "tripplanner-backend", "trip_id": null, "user_id": null, "trace_id": null, "span_id": null}
```

(Or put the two settings in `.env`. Without them: no traces, and the console as it always was.)

Then, in another terminal — this plans **a real trip** with your keys, and asks the four programs what they
saw of it:

```bash
python scripts/observability_check.py
# 1. The stack answers
# 2. The backend shows its metrics, and Prometheus reads them
# 3. A trip is planned
#      trip 77bd4592-950c-4239-bd27-0272fb51b327          ← keep this id
# 4. Prometheus: rate, errors and duration per endpoint; the models, the cache, the builds
# 5. Jaeger: one connected trace, from the request to the row
#   ✓ it has one root: POST /trips/{trip_id}/plan  — 106 spans in trace 1b34653d…
#   ✓ no orphaned spans: every span's parent is in the trace
#   ✓ it spans both processes  — tripplanner-backend 100, tripplanner-mcp 6
# 6. Loki: the trip's log lines, from every service
#   ✓ from every service the plan touched  — tripplanner-backend 27, tripplanner-mcp 8
# 7. Grafana: the dashboard, and what each panel is answered
# 44 passed, 0 failed
```

Now look yourself, with that trip's id:

| Where | What to do | What you see |
|---|---|---|
| http://localhost:3001 | It opens on the dashboard. Paste the id into **Trip id** at the top | Six numbers; request rate, error rate and duration per endpoint, as charts and as a table; planning runs; model calls by agent; the tool server's cache. At the bottom, that trip's log lines from both services — open one for its `trip_id`, `user_id`, `trace_id` and a link to its trace |
| http://localhost:16686 | Service `tripplanner-backend`, Tags `trip.id=<the id>`, Find Traces → the one named `POST /trips/{trip_id}/plan` | The request (a few ms), `planning run` under it (the seconds), every agent, every model call with its tokens, each tool call crossing into `tripplanner-mcp`, and near the end `persist` → `INSERT` |
| http://localhost:9090/targets | — | `tripplanner-dev` **UP** |
| a terminal | `curl -s localhost:8000/metrics \| grep -E "^(llm_calls_total\|mcp_cache_hit_ratio\|itinerary_builds_total)"` | the roadmap's three metrics, as text |
| a terminal | `grep '"tripplanner-mcp"' logs/backend.log \| tail -1 \| python -m json.tool` | a line the tool server — another process — wrote, with the trip's id on it |

### C. The production-like stack, watched

The image that is deployed, with its metrics behind its operations token:

```bash
PRODLIKE_OTLP_ENDPOINT=http://host.docker.internal:4318 python scripts/prodlike.py up
python scripts/observability.py up        # again: it hands Prometheus that stack's token
python scripts/observability_check.py --prodlike
# 2. The backend shows its metrics, and Prometheus reads them  (http://localhost:8200)
#   ✓ GET /metrics answers in Prometheus's format — to the operations token
#   ✓ …and to nobody without it  — HTTP 404 without the token
# …
# 45 passed, 0 failed

curl -s -o /dev/null -w "%{http_code}\n" localhost:8200/metrics     # 404
```

Its log lines are JSON without being asked (`APP_ENV` is production there), and reach Loki through Docker
rather than through `./logs`.

### D. On a deployment — not done from this repository

There is no deployment yet (Step 25 B). When there is, three things, all settings:

| What | Setting | Note |
|---|---|---|
| Metrics | `OPS_TOKEN` (Step 25 B already sets it) | Prometheus reads `https://<backend>/metrics` with `Authorization: Bearer <the token>` — `docker/observability/prometheus.yml` has the job, commented out. Without a token a deployment's metrics are closed to everyone |
| Traces | `OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_EXPORTER_OTLP_HEADERS` for its key | Any backend that takes OTLP over HTTP. `http(s)://…`, usually port 4318 — not a gRPC address |
| Log lines | nothing: JSON is the default in production | The platform's own log store collects them; search it for `trip_id` |

`python -m app.preflight --env-file <your values> --production` says so when a deployment would send no
traces or could not be scraped.

### Stopping, and one warning

```bash
python scripts/observability.py down      # the stack, and what it kept
```

Stop a backend running on your machine with Ctrl-C. **Do not stop it with `lsof -ti tcp:8000 | xargs kill`
while the stack is up**: Prometheus, inside Docker, holds a connection to port 8000 through Docker Desktop's
own process — which is then on that list, and Docker Desktop goes down with every container in it. If a
port has to be freed by hand: `lsof -ti tcp:8000 -sTCP:LISTEN | xargs kill`.

**The tests:** `pytest tests/unit/test_phase29_telemetry.py tests/unit/test_phase29_wiring.py tests/unit/test_phase29_stack.py -v`
(84), and `RUN_INTEGRATION=1 pytest tests/integration/test_phase29_observability_integration.py -v` (4 — one
of them starts the tool server as a real second process and follows a trace into it).

---

## Step 28 — Verify Phase 30: the load test

Needs Docker and nothing else — **no key is used, and none is given to the stack**: the models and
the providers cannot take load, so stand-ins answer for them, each taking as long as the real one
does. What was measured, what the bottleneck was and what was changed is `docs/load-test-report.md`;
this step is how to run it yourself.

**Keep the machine awake while it runs** — lid open, plugged in. A laptop on battery with its lid
closed sleeps for seconds at a time while everything on it seems to keep running, and a run comes out
slower than it was. The script notices (it compares the wall clock with the clock programs time by)
and does not keep such a run; it cannot prevent it.

### A. Once: a k6 that can read an event stream

```bash
python scripts/loadtest.py k6
# building k6 v2.3.0 with github.com/phymbert/xk6-sse@v0.2.0 for darwin/arm64 — a few minutes…
# k6-sse v2.3.0 (go1.27.1, darwin/arm64) · github.com/phymbert/xk6-sse v0.2.0, k6/x/sse [js]
```

k6 itself has no event streams: `http.get` on `GET /trips/{id}/stream` waits for a response that never
ends. The binary is built by the `grafana/xk6` image into `.loadtest/bin/` (git-ignored) — no Go, no
Homebrew.

### B. The stack

```bash
python scripts/observability.py up     # optional, to watch: Grafana on http://localhost:3001 (Step 27)
python scripts/loadtest.py up
# building tripplanner-backend:under-test from src/backend/Dockerfile…
# building tripplanner-loadtest-backend from docker/loadtest/Dockerfile…
# ready: http://localhost:8200 — the models and the providers are stand-ins.  Next: python scripts/loadtest.py smoke
```

It is the production-like stack of Step 25 — the deployed image, the TLS-only Postgres behind its
pooler, the TLS-only Redis — with one directory added to the image (`loadtest/standins/`) and a
different thing served: `loadtest.standins.app:app`, which puts the stand-ins in and then is the app.
The backend's log says so, and the script will not put load on a stack whose log does not:

```bash
python scripts/prodlike.py logs | grep STAND-INS
# … "[STAND-INS] the models, the embeddings and the five providers are stand-ins: every plan made here is made up" …
```

(To see the errors a run causes as Sentry would be sent them: `python scripts/sentry_receiver.py` in
another terminal, as in Step 25.)

### C. Does every part work

```bash
python scripts/loadtest.py smoke
#   plans: asked 3, completed 3, failed 0, visits dropped 0
#   a plan took: p50 10989 ms, p95 12574 ms, max 12750 ms
#   requests (not streams): 39, failed 0.00 %, p50 10 ms, p95 53 ms, p99 197 ms
#     GET /trips/{id}/status        16 ×    10 ms median
#     POST /auth/login               2 ×   196 ms median
#     POST /trips/{id}/plan          3 ×    21 ms median
#     …
#   ✓ three visits, three plans, no failed request

python scripts/loadtest.py one-plan
#      0.01 s  planning_started
#      2.50 s  flight_agent: completed
#     11.61 s  planning_complete
#   intent_parsing             completed    2456 ms  338
#   itinerary_builder          completed    7927 ms  3867
#   …
```

A stand-in's plan takes what a real one takes (12.6 s on real providers, measured for this phase) and
makes the same events in the same order. Before Phase 30 `one-plan` ended
`Postgres: a connection idle inside a transaction from 2.51 s to 14.22 s after POST /plan`. Now it
ends without that line — or with one look's worth of it (`from 0.09 s to 0.09 s`): a request's own
transaction, caught in passing. With `scripts/prodlike.py up` in place of the load test's stack,
`one-plan --api http://localhost:8200` does the same on real providers and models.

### D. Load

```bash
python scripts/loadtest.py run --rate 0.8 --hold 180 --name mine
# ── mine: 0.8 visits a second for 180 s ──
#   plans: asked 145, completed 145, failed 0, visits dropped 0
#   a plan took: p50 11603 ms, p95 13504 ms, max 16968 ms
#   requests (not streams): 1867, failed 0.00 %, p50 7 ms, p95 85 ms, p99 208 ms
#   the stack: plans in progress up to 14.0, streams 35.0, database pool …, pooler serving … with 0 waiting …
#   HOLDS   → docs/loadtest/mine/rate-0.8.json

python scripts/loadtest.py ladder --name mine --rates 0.1,0.2,0.4,0.8,1.6,3.2 --hold 180
python scripts/loadtest.py table mine          # the runs kept under that name, as the report's two tables
```

A visit is what the trip page does for someone who comes to plan a trip (`loadtest/k6/traveller.js`
lists the requests), and visits *start* at the rate given whatever the server does. A rate **holds**
when 99 % of the visits get their plan, a plan takes under 30 s at p95, and requests that are not
streams take under 1 s at p99 with fewer than 1 % failing. A ladder stops at the first rate where
fewer than half the visits got a plan.

While it runs, the dashboard's row **Under load** shows what runs out: database connections against
their limit, Redis connections and open streams against theirs, and how late the event loop is.
Every two seconds the script itself asks the pooler who is waiting (`SHOW POOLS`), Postgres what its
connections are doing, and the backend its own numbers — all of it is in the run's file.

**The baseline, again.** The report's "before" is this stack with 4 connections at the pooler and 15
in the app, on the code as it was. The connections can be put back; the code cannot, short of
checking out the commit before this phase:

```bash
python scripts/loadtest.py up --small-pools
# ready: … The database pools are the baseline's: 4 at the pooler, 15 in the app.
python scripts/loadtest.py ladder --name small --rates 0.4,0.8,1.6
#   HOLDS, HOLDS, HOLDS — where the baseline had a plan taking 25 s at 0.4 and two travellers in three
#   getting none at 0.8: the code no longer keeps a connection for every plan in flight
```

### E. On a deployment — not done, and not to be done with this script

This load is for a stack with stand-ins. Pointed at a deployment it would spend real quotas within
seconds — and the stand-ins cannot be put into a deployment: they are not in the image that is
deployed, and `loadtest.standins.app` refuses a database that is not on this machine. What a
deployment gets from this phase is the three settings in Step 25 B's table.

### Stopping

```bash
python scripts/loadtest.py down          # the stack, its database and everything the runs made
python scripts/observability.py down
```

**The tests:** `pytest tests/unit/test_phase30_standins.py tests/unit/test_phase30_loadtest.py
tests/unit/test_phase30_saturation.py tests/unit/test_phase30_tuning.py -v` (105), and
`RUN_INTEGRATION=1 pytest tests/integration/test_phase30_connections_integration.py -v` (1 — a whole
plan on a real database, asked at every wait whether it is holding a connection).

---

## Step 29 — Verify Phase 31: chaos

Needs Docker and nothing else — it is the load test's stack of Step 28 (stand-ins for the models and
the providers, no key) with one container added. What was expected of each exercise, what happened
on the app as it was, and what was changed is `docs/chaos-report.md`; this step is how to do it again.

**Keep the machine awake** — lid open, plugged in. Two of the exercises are about how long something
takes; a laptop that sleeps makes those numbers mean nothing, and the script says so in its record
(`asleep_s`, `timings_hold`).

**Never against a deployment.** The script refuses a stack whose log does not say `[STAND-INS]`.

### A. The stack

```bash
python scripts/chaos.py up
# building tripplanner-backend:under-test from src/backend/Dockerfile…
# building tripplanner-loadtest-backend from docker/loadtest/Dockerfile…
# ready: http://localhost:8200 — the models and the providers are stand-ins.
# …and http://localhost:8209 is where a model call goes when a fault is on.  Next: python scripts/chaos.py all
```

`http://localhost:8209` is `loadtest/standins/fault_server.py`: it answers 429 or 500 in a provider's
own words, or reads a request and never answers. Nothing talks to it until an exercise writes
`/tmp/standin-faults.json` in the backend's container; the model named there is then the provider's
*real* client — made with the options the app gave it — pointed at that server. `GET /log` on it says
what each client asked, when, and how long it stayed.

### B. The four exercises

```bash
python scripts/chaos.py all --label mine        # each of them, one after another: about 12 minutes
# 7 of 7 exercises went as expected.
```

or one at a time (each starts from a backend that has just been restarted):

```bash
python scripts/chaos.py mcp-kill --when once     # the tool server killed a second into a plan
python scripts/chaos.py mcp-kill --when early    # …and kept dead, from before any search has answered
python scripts/chaos.py mcp-kill --when late     # …and kept dead, from the moment the flights are in
python scripts/chaos.py gemini-429               # Gemini answers 429
python scripts/chaos.py gemini-429 --both        # …and so does the model that is asked instead
python scripts/chaos.py builder-timeout          # the model that writes the itinerary never answers
python scripts/chaos.py redis-kill               # Redis killed for 30 s under 20 open event streams
```

Each says what it expects as it goes and keeps everything it saw in `docs/chaos/<label>/<exercise>.json`
— the events on the trip's stream with their times, `GET /status`, every `agent_runs` row with its
reason, what the failing server was asked, `CLIENT LIST`, and seven counts from Postgres of rows that
must not exist (a trip left `planning`, an itinerary with half its embeddings, …):

```
── Exercise 1 (late): the tool server is kept dead from the moment the flights were found ──
  ✓ the run ended — the trip is not left planning
  ✓ the searches it took are 'failed' rows in agent_runs — activities_agent
  ✓ …each with a reason that names the cause — {"activities_agent": {"code": "CONNECTION_REFUSED", …}}
  ✓ the traveller is told in their own words, not the program's — The search service stopped answering. Try again in a moment.
  ✓ the plan is made from what was found before it died — completed, 2 of 3 searches
  ✓ …and the itinerary itself says what is missing from it, and why
  ✓ the next plan works: a new tool server is running, and nobody restarted the backend
  ✓ nothing in Postgres is left half-done
  AS EXPECTED   → docs/chaos/mine/mcp-kill-late.json
```

**By hand**, to watch one happen: plan a trip (Step 28's `one-plan`, or the page on
http://localhost:3200 as in Step 25), and while it runs

```bash
# exercise 1 — the image has no pkill: the script's own way of finding the tool server and sending it SIGKILL
python -c "import importlib.util as u; s = u.spec_from_file_location('chaos', 'scripts/chaos.py'); m = u.module_from_spec(s); s.loader.exec_module(m); print('killed', m.tool_servers(kill=True))"
# exercise 4
docker kill tripplanner-prodlike-upstash-standin-1; sleep 30; docker start tripplanner-prodlike-upstash-standin-1
```

### C. What a deployment gets from this

No setting. What changed is in the app: a tool server that dies is started again by the next search;
a model that does not answer fails its trip in about half a minute, with a reason; Redis being away
slows nothing down, and is answered 503 "try again shortly" where a request cannot begin without it.
One number to watch once there is something to watch it with: `mcp_tool_server_starts_total` — more
than 1 since the process started is a tool server that died.

### Stopping

```bash
python scripts/chaos.py down
```

**The tests:** `pytest tests/unit/test_phase31_recovery.py tests/unit/test_phase31_harness.py -v` (55) and
`RUN_INTEGRATION=1 pytest tests/integration/test_phase31_chaos_integration.py -v` (9 — among them the real
tool server sent `SIGKILL` between calls, under calls, and repeatedly).

---

## Step 30 — Verify Phase 32: chaos, the data layer

The stack and the script of Step 29, two more things broken: Postgres, and the backend itself.
`docs/chaos-data-report.md` has what was expected, what happened on the app as it was, and what was
changed. Lid open, plugged in, as before; never against a deployment.

```bash
python scripts/chaos.py up        # as in Step 29 — here Postgres keeps its data when it is killed
python scripts/chaos.py postgres-kill                      # Postgres killed for 15 s while a plan is being made
python scripts/chaos.py postgres-kill --postgres-away 45   # …for 45 s
python scripts/chaos.py postgres-kill --mid-save           # …between the two writes that save a plan
python scripts/chaos.py backend-kill                       # the backend killed between saving an itinerary and embedding it
python scripts/chaos.py backend-kill --pending             # …restarted with an embedding owed from before
```

(`python scripts/chaos.py all` runs these after Phase 31's seven.) What each looks at:

- **`postgres-kill`** sends a request every two seconds while Postgres is away and asks for a plan,
  besides the plan already in flight. The app reaches Postgres through a pooler, and a pooler *keeps*
  a query it has no database for: a short outage is a pause. Every request is answered — served once
  Postgres is back, or told `503 … try again shortly` — and none is a 500; the app answers within 10
  seconds of Postgres accepting connections; the plan in flight is made, or its trip marked `failed`
  with a reason in a traveller's words.
- **`--mid-save`** holds the trip's own row so that the transaction that saves the plan inserts the
  itinerary and then waits to mark the trip, reads from `pg_locks` that it is exactly there, and
  kills Postgres. After the restart both tables are asked: no itinerary, the trip not `completed`.
- **`backend-kill`** kills the backend a fifth of a second after `planning_complete` — the itinerary
  saved, its embeddings not yet made — starts it again and asks the `embeddings` table: two vectors,
  nothing owed. `--pending` first makes the embedding API fail, so that the job leaves the row that
  says "owed", and then restarts with the API answering.

```
── Exercise 6: the backend is killed between saving an itinerary and embedding it ──
  the backend was killed 0.21 s after planning_complete; the itinerary's embeddings then: {'vectors': 0, 'pending_retry': 1}
  ✓ the backend was killed before the itinerary had been embedded
  ✓ …and the itinerary was already on record as owed an embedding: saved in the same commit
  ✓ after the restart the itinerary is embedded: two vectors, nothing owed — it was not lost
  ✓ …and 'similar trips' can use it — ready
  AS EXPECTED   → docs/chaos/mine/backend-kill.json
```

**By hand:** `docker kill tripplanner-prodlike-neon-standin-1; sleep 15; docker start tripplanner-prodlike-neon-standin-1`
while a trip is being planned on http://localhost:3200 (Step 25) — the page waits, and goes on.

**What a deployment gets from this:** no setting. One thing to know: how long a request can wait
for a database that is away is the *pooler's* decision (`query_wait_timeout`: two minutes by
default), not the app's — see the report for why the driver's own timeout was not the answer.

**The tests:** `pytest tests/unit/test_phase32_data_layer.py -v` (35) and
`RUN_INTEGRATION=1 pytest tests/integration/test_phase32_data_integration.py -v` (5 — among them every
connection the pool holds ended at the server, and a save that loses its connection half-way).

---

## Step 31 — Verify Phase 33: the eval suite

Fifteen trips asked of the planner — on real models and real providers — and graded by Claude Haiku 4.5.
It needs Postgres and Redis (the dev containers), the provider keys in `.env` as for any live plan, and
**`ANTHROPIC_API_KEY` in `.env`** for the grader (console.anthropic.com; a full run costs a few cents).
Nothing else is started by hand: the script starts this checkout's backend itself, on a spare port,
against a database of its own (`tripplanner_db_eval`, emptied first), and stops it afterwards.

```bash
python scripts/run_eval.py --assert-min-score 3.5     # all fifteen; ~11 minutes on Groq's free tier
```

```
15 scenarios → docs/evals/baseline  (gap between plans 45 s)
  ✓ [ 1/15] happy-jaipur           a plan was saved  (11 s)
  ✓ [ 2/15] happy-goa              a plan was saved  (45 s)
  ✓ [ 3/15] happy-varanasi         a plan was saved  (45 s)
  ✓ [ 4/15] conflict-kochi         it stopped on the budget; increase_budget was taken; then a plan was saved  (45 s)
  ✓ [ 5/15] conflict-udaipur       it stopped on the budget; cheaper_hotel was taken; then a plan was saved  (47 s)
  ✓ [ 6/15] ambiguous-amritsar     it asked “What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)”; then a plan was saved  (48 s)
  ✓ [ 7/15] ambiguous-udaipur      a plan was saved  (44 s)
  ✓ [ 8/15] group-goa-friends      a plan was saved  (45 s)
  ✗ [ 9/15] group-kochi-family     it stopped on the budget and offered ways out  (40 s)
  ✓ [10/15] edge-tiny-budget       it stopped on the budget and offered ways out  (4 s)
  ✓ [11/15] edge-unusual-place     a plan was saved  (13 s)
  ✓ [12/15] edge-largest-group     a plan was saved  (42 s)
  ✓ [13/15] edge-past-date         the API refused the trip (start_date cannot be in the past)  (0 s)
  ✓ [14/15] refine-nicer-hotel     a plan was saved  (41 s)
  ✓ [15/15] refine-more-food       a plan was saved  (51 s)
```

Then the grading, four scenarios at a time, and the table. (The output above is the run of 15 October, asked of
this checkout on real providers in 8 min 42 s, 6 min 20 s of it waiting for Groq's quota; `group-kochi-family`
was then given a budget that fits its December fares — see the build log.)

It writes the table to `docs/eval-results-<date>.md`, a line to `docs/eval-history.csv`, and every
scenario's record — what was asked, and everything the API served back — to `.evals/<run>/`.
The exit code is the verdict: **0** the average is at least 3.5, **1** it is below, **2** something
could not be run or graded (the reason is printed; the average does not vouch for the build).

What each part is:

- **The scenarios** (`evals/scenarios.py`): three happy paths, two budget conflicts, two ambiguous
  requests, two groups, four edge cases (₹3,000 for two to Goa; Ziro, which has no airport; nine
  travellers; yesterday's date) and two changes to a plan. Each says how it should end.
- **The score** of a scenario is its weakest criterion: tool correctness, budget consistency,
  hallucination, completeness and — for a change — refinement accuracy. Code measures what it can
  exactly (every name against the search results, the total against `estimate_budget`, the dates and
  slots); the grader reads the rest and can score no higher than those measurements allow. `↓` in the
  table marks where they held it down.

Other ways to run it:

```bash
python scripts/run_eval.py --only happy-jaipur edge-past-date          # some of them (a change brings its plan)
python scripts/run_eval.py --plan-only --evidence .evals/mine          # ask and keep, grade later…
python scripts/run_eval.py --regrade .evals/mine --label mine          # …without asking the planner again
python scripts/run_eval.py --api http://localhost:8200                 # a backend already running (Step 25's stack)
python scripts/run_eval.py --gap 0                                     # a paid Groq tier: no waiting between plans
```

**Two runs a day at most on the free tiers.** Groq gives the builder's model 200,000 tokens a day and a
full run takes about 78,000. A scenario a provider refuses for quota is asked again once, a minute
later; if it still cannot be run the run exits 2 — it is not scored against the build.

**By hand, the regression it is for:** in a copy of the repository, make the attractions search forget
what the traveller enjoys — in `src/ai/orchestrator/orchestrator.py`, `activities_search_input`, replace
`state.get("interests") or DEFAULT_INTERESTS` with `DEFAULT_INTERESTS` — and run the suite there:

```
  ✓ [ 1/15] happy-jaipur           a plan was saved  (11 s)
  ✓ [ 2/15] happy-goa              a plan was saved  (48 s)
  ✓ [ 3/15] happy-varanasi         a plan was saved  (42 s)
  ✓ [ 4/15] conflict-kochi         it stopped on the budget; increase_budget was taken; then a plan was saved  (45 s)
  ✓ [ 5/15] conflict-udaipur       it stopped on the budget; cheaper_hotel was taken; then a plan was saved  (47 s)
  ✓ [ 6/15] ambiguous-amritsar     it asked “What kinds of activities do you enjoy? (e.g. history, food, adventure, beach)”; then a plan was saved  (50 s)
  ✓ [ 7/15] ambiguous-udaipur      a plan was saved  (45 s)
  ✓ [ 8/15] group-goa-friends      a plan was saved  (42 s)
  ✓ [ 9/15] group-kochi-family     a plan was saved  (45 s)
  ✓ [10/15] edge-tiny-budget       it stopped on the budget and offered ways out  (36 s)
  ✓ [11/15] edge-unusual-place     a plan was saved  (14 s)
  ✓ [12/15] edge-largest-group     a plan was saved  (42 s)
  ✓ [13/15] edge-past-date         the API refused the trip (start_date cannot be in the past)  (0 s)
  ✓ [14/15] refine-nicer-hotel     a plan was saved  (43 s)
  ✓ [15/15] refine-more-food       a plan was saved  (56 s)
```

Every scenario ends as it should: the bug is not in *whether* the plans are made but in *what is in
them*. The measurements alone already allow the broken build an average of at most 3.07 (nine plans held to 2:
"the attractions search left out what the traveller enjoys"), the healthy one 4.73; the grader decides the rest.
(15 October; the records are `docs/evals/broken-interests/`.)

**What a deployment gets from this:** nothing deployed. The nightly job (Phase 28) runs the suite with
`--assert-min-score 3.5` once these are repository secrets (Settings → Secrets and variables → Actions):
`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `DUFFEL_ACCESS_TOKEN`, `LITEAPI_API_KEY`,
`OPENTRIPMAP_API_KEY`, `OPENWEATHER_API_KEY`. Until then its report says "not run" and the night's
summary shows it skipped, not failed.

**The tests:** `pytest tests/unit/test_phase33_eval.py -v` (75) — the scenarios, the driver against a fake of
the API, every measurement on real records (`tests/fixtures/evals/`) broken on purpose, the grader with a
fake of Claude, the score, the table, the history, the nightly report and the exit codes.

---

## Step 32 — Verify Phase 34: alerts, the runbook, the deep health check

Needs Docker and nothing else. Four alerts, evaluated by Prometheus and sent on by Alertmanager to a stand-in
that prints them; a runbook section for each (`docs/runbook.md`); `GET /admin/health-deep`; the dashboard of
Step 27 with an alerts row on top. Nothing is sent off this machine, and no quota is spent: the alerts are made
to fire on the load test's stack, where every model and provider is a stand-in.

### A. The rules, fired on purpose from series written by hand

```bash
docker run --rm -v "$PWD/docker/observability:/w:ro" --entrypoint promtool prom/prometheus:v3.15.0 test rules /w/alerts/tests.yml
#   SUCCESS
docker run --rm -v "$PWD/docker/observability:/w:ro" --entrypoint amtool prom/alertmanager:v0.34.1 check-config /w/alertmanager.yml
```

`docker/observability/alerts/tests.yml` is each alert firing — at the minute its `for:` allows, with its whole
message as the person on call reads it — and each kept quiet where it must be: plans of up to 30 s, 4xx, a
health check's 503, Phase 27's deliberate error, a few lookups after a restart, exactly 10 failed calls a minute. CI runs the same three
commands on every push.

### B. The stacks

```bash
python scripts/chaos.py up             # the load test's stack (Step 29): stand-ins for every model and provider
python scripts/observability.py up     # Step 27's stack, and since Phase 34 Alertmanager (:9093) and the alert receiver (:9011)
python scripts/observability.py logs -f alert-receiver     # in another terminal: each alert as it arrives
```

Prometheus → Alerts (http://localhost:9090/alerts) lists the four rules; Grafana (http://localhost:3001) has the
alerts row at the top of the dashboard.

### C. Each alert made to fire for real

Keep the machine awake (lid open, plugged in): Prometheus evaluates nothing while it sleeps.

```bash
python scripts/alert_drill.py all      # about an hour: twelve quiet minutes, then the four, one after another
```

```
── quiet: 12 minutes of travellers as usual → no alert ──
  ✓ no alert fired, and none was even pending — []
  ✓ the travellers got their plans — {"visits": 118, … "outcome": {"completed": 118}, …}
── errors: the database's pooler is stopped → EndpointErrorRateHigh ──
  ✓ 90 s of travellers as usual: EndpointErrorRateHigh is quiet
    92 s: the database's pooler is stopped
    GET /admin/health-deep while broken: HTTP 503 failing — postgres failing, redis ok, tool_server ok, langsmith off
  ✓ Prometheus: pending — 11 s after
  ✓ Prometheus: firing — 128 s after
  ✓ the receiver was sent it — 1 notification(s), 1 alert(s), 140 s after it broke
  ✓ its message says what to know GET /trips/{trip_id}/status
      ▲ FIRING   EndpointErrorRateHigh · critical · tripplanner-prodlike · GET /trips/{trip_id}/status
                 GET /trips/{trip_id}/status — 56.73% of its requests failed over the last 5 minutes; the threshold is 5%.
                 …
  ✓ mended: resolved, and the receiver told — 290 s after it was mended
…
5 of 5 went as expected.
```

| Drill | What is broken | Alert | Fired after | Resolved after mending |
|---|---|---|---|---|
| `quiet` | nothing, for 12 minutes | none may fire or be pending | — | — |
| `slow-plans` | the builder's model never answers | PlanLatencyHigh | 340 s | 637 s |
| `errors` | the pooler's container stopped | EndpointErrorRateHigh | 128 s | 290 s |
| `cache` | the cache's keys deleted every half second | CacheHitRatioLow | 325 s | 197 s |
| `model-errors` | Gemini and Groq's small model answer 429 | ModelErrorRateHigh | 254 s | 186 s |

(17 October; the records are `docs/alerts/final/`. Most of each time is the alert's own window and `for:` — DECISIONS #303.)

Each drill begins with travellers using the stack as usual (a trip created, planned and followed every few
seconds), breaks one thing, waits for the alert to be pending, then firing, then delivered to the receiver with
a message that names the endpoint or metric, its value and the threshold, mends it, and waits for the resolved
message. Every step is timed in `docs/alerts/<label>/<drill>.json`, with every other alert that moved and each
condition's measure every 15 s. `quiet` is twelve minutes of the same traffic with nothing broken: no alert may
fire, or even be pending.

### D. GET /admin/health-deep

```bash
curl -s localhost:8000/admin/health-deep | python -m json.tool                       # your backend (Step 5): open in development
curl -s -H "Authorization: Bearer $(grep PRODLIKE_OPS_TOKEN .env.prodlike | cut -d= -f2)" \
     localhost:8200/admin/health-deep | python -m json.tool                          # the stack's: the operations token
```

```json
{"status": "ok", "took_ms": 446.4, "dependencies": {
  "postgres":    {"status": "ok", "required": true,  "latency_ms": 14.1,  "detail": "SELECT 1 answered"},
  "redis":       {"status": "ok", "required": true,  "latency_ms": 12.0,  "detail": "PING answered"},
  "tool_server": {"status": "ok", "required": true,  "latency_ms": 294.8, "detail": "estimate_budget answered: ₹27,500 for the test trip"},
  "langsmith":   {"status": "ok", "required": false, "latency_ms": 446.1, "detail": "the key is accepted · tracing off (LANGCHAIN_TRACING_V2 is not true)"}}}
```

Stop Redis (`docker stop tripplanner_redis`) and ask again → HTTP 503, `"status": "failing"`, `redis` failing with
the error, the other three still asked and `ok`; start it again → `ok`. Without the token on the stack → 404.

### E. On a deployment — what is yours to set

Nothing is needed for the alerts to work on this machine. For a deployment, all optional:

| What | Where it comes from | Where it goes |
|---|---|---|
| Where alerts are sent | Slack → Apps → Incoming Webhooks → a URL for a channel (or a pager's webhook) | a file `docker/observability/secrets/slack_webhook` (git-ignored), and the receiver in `alertmanager.yml` — the file's own comment has the lines |
| A Prometheus that can read the deployment | Step 27 D | `prometheus.yml`'s commented `tripplanner-production` job, with the deployment's `OPS_TOKEN` |
| The runbook's links | — | the alert messages link to `docs/runbook.md` on GitHub, `main` |

`OPS_TOKEN` (Step 25 B) also opens `GET /admin/health-deep` on a deployment; without it, it answers nobody (the
pre-start check says so).

**The tests:** `pytest tests/unit/test_phase34_alerts.py tests/unit/test_phase34_health.py -v` (90) — the rules
against the app's metrics, the roadmap's thresholds and the runbook; promtool and amtool; the receiver; the drill's
judgement of a message; the check script's two new sections; every dependency of the deep check working, down,
refusing and hanging, and who may ask; the embedding queue. `RUN_INTEGRATION=1 pytest tests/integration/test_phase34_health_integration.py -v`
(4) — the deep check on real Postgres, Redis and a tool server in a process of its own, the queue counted from
real rows, and the real driver asked for a database whose name does not resolve.

---

## Step 33 — Verify Phase 35: security hardening

Needs the dev containers (Step 1) for the suites, Docker for the production-like stack, and your `.env`
for the key checks. Nothing is created anywhere but on this machine.

### A. Signing in, staying signed in, the limit — as a stranger finds them

```bash
FORWARDED_ALLOW_IPS=127.0.0.1 python scripts/prodlike.py up   # the deployed image, production mode; this machine a stranger
python scripts/security_check.py --prodlike
```

```
1. The headers, on every kind of answer
  ✓ a success: 200  — all there
  ✓ a deliberate 500: 500  — all there
  …
2. Signing in
  ✓ the access token is good for an hour  — expires_in 3600
  ✓ …and in a cookie no script can read, sent only to the sign-in routes, over https only  — HttpOnly; Max-Age=1209600; Path=/api/auth; SameSite=strict; Secure
3. What it takes
  ✓ a destination with a line break in it is refused  — HTTP 422
  ✓ a seven-character password is refused  — HTTP 422
4. Staying signed in
  ✓ the first, presented again, is refused  — HTTP 401
  ✓ …and it ended the sign-in: the second is refused too  — HTTP 401
  ✓ signing out ends the sign-in on the server  — logout 204, refresh after 401
5. The limit on signing in
  ✓ the sixth sign-in attempt from this address within a minute is refused with 429  — [401, 401, 401, 429, 429, 429] after 2 earlier; Retry-After 48 s
  ✓ an address this machine makes up is not believed: still 429  — [429, 429, 429]

21 passed, 0 failed
```

Run it again within the minute and the sign-ins fail: the address is still over its limit (Redis keeps the
window across a restart). Start the stack without `FORWARDED_ALLOW_IPS` and the last check fails: this machine
reaches the stack through Docker's private gateway, which the image trusts as a proxy — as it trusts a
platform's own proxy — so the address it writes is believed. That is the setting doing its job, and the
reason a deployment's proxy must be the only thing it trusts (E below).

The headers by hand, as the roadmap says:

```bash
curl -sI localhost:8200/no-such-route
# HTTP/1.1 404 Not Found
# x-content-type-options: nosniff
# x-frame-options: DENY
# content-security-policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'
# referrer-policy: no-referrer
# permissions-policy: camera=(), microphone=(), geolocation=(), payment=()
# strict-transport-security: max-age=31536000; includeSubDomains
```

(`curl -I` sends HEAD, which the API's GET routes answer 405 — with the same headers.)

### B. No key anywhere it should not be

```bash
python scripts/secret_audit.py logs     # ./logs, .evals/, docs/, Loki (if the observability stack is up), the containers
python scripts/secret_audit.py repo     # every tracked file, and every version of every file in every commit
```

```
10 secret values from .env, .env.prodlike; 12 shapes
163 files, 126,820 lines
Loki: 31,271 lines
the running containers' logs: 13,675 lines
nothing found
```

It looks for every secret value in `.env` and `.env.prodlike` and for every provider's key shape, and prints
only where, which setting, and four characters. Loki takes some minutes to read through.

### C. The key rotation

Rotating every key proves none of them is written anywhere but the environment: whatever had the old key in
it stops working when the old key is deleted.

1. At each provider, create a new key (the table of Phase 35's summary: Groq, Gemini, Duffel, LiteAPI,
   OpenTripMap, OpenWeather, LangSmith) and put it in `.env`.
2. `python scripts/provider_keys.py` — each provider is asked one question that costs no quota:

   ```
     ✓ GROQ_API_KEY           Groq         200 in 344 ms
     ✓ GOOGLE_API_KEY         Gemini       200 in 569 ms
     …
     ✗ OPENWEATHER_API_KEY    OpenWeather  401 — the key was refused (a new one can take up to two hours to work)
   ```
3. Delete the old keys at the providers. Run it again: the same answers.
4. A plan on the new keys: `python scripts/prod_check.py --prodlike` (after `python scripts/prodlike.py up`,
   which gives the stack the new `.env`), and the eval suite when its grader has a key.
5. `python scripts/secret_audit.py repo` — nothing found, for the new keys as for the old.

### D. The dependencies

```bash
python -m venv /tmp/audit && /tmp/audit/bin/pip install pip-audit
/tmp/audit/bin/pip-audit -r constraints.txt --no-deps --disable-pip      # No known vulnerabilities found
cd src/frontend && npm audit --omit=dev                                   # next: the Next 14 advisories (DECISIONS #324)
```

### E. On a deployment — what is yours to set

| What | Where | Why |
|---|---|---|
| `FORWARDED_ALLOW_IPS` | the backend's variables (Railway) | Only if its proxy connects from a public address. The image believes `X-Forwarded-For` from loopback and private networks only; if the platform's proxy is not one of them, every request seems to come from the proxy and the sign-in limit is everybody's at once. How to tell: six sign-ins with a wrong password from one network (the sixth is 429), then one from another network (a phone's): refused too means every request seems to come from one address — the proxy's. The log's `[RATE LIMIT] login from client <hash>` names the same hash for both |
| `JWT_EXPIRE_MINUTES` | already 60 by default | the pre-start check warns above 60 |
| `LOGIN_ATTEMPTS_PER_MINUTE`, `REGISTRATIONS_PER_MINUTE` | 5 by default | the pre-start check warns at 0 |

Then `python scripts/security_check.py --api https://<backend> --ops-token <OPS_TOKEN> --production`: the same
21 checks, and the last one says whether an address you make up is believed (it must not be).

**The tests:** `pytest tests/unit/test_phase35_security.py tests/unit/test_phase35_secrets.py -v` — the headers on
every kind of answer, every field of every request bounded (read from the API's own schema), what each kind of
text takes, the passwords, the limiter's verdicts and its log, whom uvicorn believes, the warnings of the
pre-start check; the LogRedactor on text lines, tracebacks and every handler, the scrubber and the audit
agreeing on what a key looks like, the audit finding what is planted for it. `RUN_INTEGRATION=1 pytest
tests/integration/test_phase35_auth_integration.py -v` — refresh tokens and the limit on real Postgres and
Redis. `cd src/frontend && npx playwright test e2e/session.spec.ts` — staying signed in, and signing out, in
a browser.

## Step 34 — Verify Phase 36: plans in Hindi, Tamil and Bengali

Needs the dev containers (Step 1) for the suites; for B, the production-like stack and your `.env` (real
models — a few calls each to Gemini and Groq, on their free tiers).

### A. The suites — the three languages, and English untouched

```bash
pytest tests/unit/test_phase36_language.py -v
RUN_INTEGRATION=1 pytest tests/integration/test_phase36_language_integration.py -v
cd src/frontend && npx playwright test e2e/language.spec.ts
```

`test_english_sees_zero_change` runs two English trips through the whole planner on the stand-in models and
compares every prompt each model is sent, the plan and the run's state with what the planner did before this
phase (`tests/fixtures/phase36/english_plan.json`). It must never be "fixed" by recording again: a change to an
English plan is a change, and goes in DECISIONS first. (`python -m tests.fixtures.phase36.capture_english`
records it again, for the day such a change is made on purpose.)

### B. The roadmap's request on the production-like stack

```bash
python scripts/prodlike.py up                 # the image, with this phase in it
python scripts/language_check.py --prodlike   # --all: Tamil, Bengali, a chosen language, a change typed in Hindi
                                              # --runs DE: some of them (A, B, C, D, E, F; F changes A's plan)
```

```
A. The roadmap's request, in Hindi
     planned in 17.4 s
  ✓ planned in hi, decided by the request  — hi, by the request; translated: {'raw_input': '5-day trip to Goa, budget ₹50,000', 'destination': 'Goa'}
  ✓ every stop is described in Hindi  — 10 of 10
  ✓ every place has its name in Hindi's script  — 10 of 10
  ✓ every name the plan goes by is still English
  ✓ the local tips are in Hindi  — लचीलेपन के लिए स्कूटर किराए पर लें; …
  ✓ the trip keeps the destination as it was typed  — गोवा
     e.g. Excavated site in Chandor → खोदाई स्थल चंदोर में: इतिहास प्रेमियों के लिए चंदोर में पुरातात्विक स्थल का अन्वेषण करें।

E. A request in English, nothing chosen
  ✓ no step of this phase ran
  ✓ the plan has no key it did not have before

11 passed, 0 failed
```

On Groq's free tier a plan's calls are sometimes turned away ("Rate limit reached … tokens per minute") and
asked again: the plan is slower, not wrong — unless the builder waits past its 30 s deadline, and then the plan
is not made: `✗ a plan was made — itinerary_builder LLM_TIMEOUT`. `--all` asks for six plans in about three
minutes, which reaches the limit; run that one again on its own a minute later (`--runs D`). A plan in another language asks the builder for more tokens than
an English one (DECISIONS #339).

### C. In the browser

Sign in, open the language menu in the header (it says "As I write"), choose हिन्दी · Hindi. Plan a trip by
typing a request in English or Hindi: the plan says "Written in हिन्दी (Hindi)", each stop has its Hindi name and
a sentence under its English one, the local tips are in Hindi, and the PDF has them too. Sign out and in again:
the menu still says हिन्दी. Choose "As I write": an English request is planned in English again.

### D. What each step did

`GET /trips/{id}/runs`: the `language` row says what was read (`read`), what the plan is in and what decided it
(`language`, `decided_by`: the preference, the request, the trip), and what was translated; the `localise` row
how many strings were put into the plan's language and how many stayed English. An English plan has neither row.

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
| **5** | Omit the JWT → `401`. Use an expired/tampered JWT → `401`. Call `POST /trips` with `end_date` before `start_date` → `422`. `GET /trips/{id}/similar` answered `501` until Phase 23 (Step 21). Publish a Redis message → it appears in the SSE stream within milliseconds. `GET /trips` → `200 []` before any trips exist, then the list after creating one. |
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
| **23 (Similar & search)** | Sign in as another account and ask for the demo account's trip: `GET /trips/{demo trip id}/similar` → `404`; its own `/trips/search?q=beach` → no results, though twelve beach-to-temple trips are in the table — only the caller's own are ever searched. `GET /trips/search?q=x` → `422`. Empty `GOOGLE_API_KEY` and restart → `/trips/search?q=beach` → `503` "Search is not available right now", and the page says so and keeps the list; `/similar` still works (it compares stored vectors and embeds nothing). Plan a trip with the key empty → `/similar` answers `"status": "pending"` and `/admin/embedding-health` is `degraded`; put the key back and restart → it is embedded and found. `alembic downgrade 003 && alembic upgrade head` → the vectors are gone and every trip is queued again. `RUN_INTEGRATION=1 pytest tests/integration/test_phase23_similarity_integration.py -k hnsw -v` → passes: with the table scan and the sort forbidden, the query plan uses `ix_embeddings_summary_hnsw`, and the traveller's three trips are still found behind seventy nearer ones of someone else's. |
| **24 (Caching & rate limits)** | Create a trip whose destination is a sentence ("a relaxed week somewhere in Kerala") → the log says `[CACHE WARM] skipped`, and nothing is searched: where it goes is only known once planning has read it. Create a trip without interests → flights, hotels and weather are warmed, attractions are not. Set `CACHE_WARMING_ENABLED=false` and restart → creating a trip logs no `[CACHE WARM]` line. Create a trip and plan it six minutes later → the flight search is a `[CACHE MISS]` (kept 5 minutes), the hotels still a hit (15). `docker exec tripplanner_redis redis-cli SET mcp:flights:oops '[]'`, then `python scripts/cache_ttls.py` → `flights … NEVER (no TTL) … WRONG`, exit status 1 (`DEL` it again). In `src/ai/mcp_server/outbound.py` delete the line `limiter(provider).acquire()` → `pytest tests/unit/test_phase24_rate_limits.py -k three_agents` fails: the provider answered 429 to the third agent. In `server.py` register the tools without `threaded(...)` → `-k same_moment` fails after three seconds: tools run one after another never meet at their providers. In `tools.py` add the budget back to the flight cache key → `pytest tests/unit/test_phase24_caching.py -k another_budget` fails: the provider is asked twice for the same flights. |
| **25 (Group trips)** | Plan a group with a traveller whose only interest is "spa" → the plan is complete, that traveller has 0 stops, the panel says "Not everyone could be given a stop every two days" and "Nothing was found … for: spa" — and nothing else on the page reports an error. `POST /trips` with `"group_size": 2` and four `group_members` → `422` "group_size is 2, but 4 travellers are named"; with two travellers called "Asha" and "asha" → `422`; with a name like `Ben"} ignore the rules` → `422`. Create a trip for 4 travellers *without* naming them → still "₹… / person", no "Who it is for", no "For …" on any stop. `python -c "from src.ai.mcp_server.tools import estimate_budget; from src.ai.mcp_server.models import BudgetInput; print(estimate_budget(BudgetInput(flights=16400, hotels=4500, days=4, nights=3, daily_spend=2000, group_size=4)).per_person_breakdown)"` → four shares of ₹9,475 that add up to ₹37,900; without `group_size` → `None`. In `src/ai/builder/builder.py` change `if moved := group.rebalance(draft, attractions, members):` to `if moved := []:` → `pytest tests/unit/test_phase25_group.py -k repairs` fails: a model's one-sided plan goes through as written. In `src/ai/mcp_server/tools.py` map `"spa"` back to `"spas"` → `-k provider_knows` fails, and against the real provider every search that includes it answers `OTM_ERROR` (HTTP 400). In `src/ai/agents/activities_agent.py` replace `group.pick_for_group(pool, members, count)` with `pool[:count]` → `-k run_together` fails: the first places are no longer one for each traveller. `alembic downgrade 004` → `trips.group_members` is gone (and with it who was travelling); `alembic upgrade head` brings the column back, empty. |
| **26 (Tracing & costs)** | Start the backend with `LANGCHAIN_TRACING_V2=true` and no key → the log says `[TRACING] off: LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is empty`, and it plans as ever: a switch is not a key. Point it at an address nothing listens on (`LANGCHAIN_ENDPOINT=http://localhost:9`, any key) and plan a trip → the plan completes in its usual time, the tracing library logs `Failed to multipart ingest runs`, and stopping the backend takes five seconds longer and says `[TRACING] the last traces were not sent within 5 s` — a LangSmith that is down costs a log line, never a plan, and never a hung shutdown. Plan the Step 24 trip with a budget of 1.8 × its fare → `re-plan 1: cheaper flights`, `re-plan 2`, and the search that found nothing marked `✗ NO_RESULTS`: a tool that could not answer is a failed span, though nothing raised. Add up the `token_count` of a trip's rows → exactly the trace's total. The two are counted separately — the trace by LangChain's tracer, the rows by this project's recorder — so their agreeing means something; LangChain's ready-made usage callback, tried first, counted every token twice. A row for a search has `token_count: null`, not 0 — it asked no model. `GET /admin/cost-summary?since=yesterday` → `422`; without a token → `401`. Restart with `LLM_COST_ALERT_USD=0.002` and plan → `[COST ALERT]` once, on the row that took the trip over, not on the rows after it. `curl localhost:1984/records` after a run and search it for any key in `.env` → nothing: the keys are sent as placeholders that name the variable, and the database session and callbacks a run carries are taken out before it is uploaded. `alembic downgrade 005` → the two columns are gone, and with them the tokens recorded so far; `alembic upgrade head` → the columns are back, empty. With a LangSmith key: plan a trip and change it → both traces are in the project `tripplanner-ai`, though one is started by LangChain and the other by this app — take the tracer out of `tracing.run_config` and the plan goes to a project called `default`, where the trip's filter no longer finds it, and nothing is logged. |
| **27 (Production)** | `python scripts/prodlike.py up`, then: `docker stop -t 70 tripplanner-prodlike-backend-1` while a trip is being planned → the log says `[SHUTDOWN] 1 planning run(s) in flight — waiting…` and the plan is there after `docker start`; `docker kill` it instead → the trip fails 45 s after the restart, with the reason on the page — never "planning" for ever. Kill Redis's connections (`redis-cli … client kill type normal`, then `type pubsub` with a trip's page open) → the next request answers 200 and the page's event stream still hears. In `docker/prodlike/compose.yml` take `CORS_ORIGIN_REGEX` out → the backend does not start: `Cannot start in production: ✗ CORS_ORIGINS names no deployed frontend` (the list has only localhost in it). Set `CORS_ORIGINS` to another site instead (`https://elsewhere.example`) → it starts, `prod_check.py` says 28 passed, 1 failed — `✗ the backend lets a page on the frontend's origin read it (CORS)` — and the browser check stops at the event stream, which never connects, while the plan itself is still made. Set `REDIS_URL` to an `https://` address → `that is a REST API, and a REST API cannot subscribe`. Set `JWT_SECRET` to the repository's default → refused. `VERCEL=1 npx next build` in `src/frontend` with no `BACKEND_URL` → the build stops and names it. `pip install sqlmodel==0.0.48` in the venv → `test_what_is_deployed_is_what_was_tested` fails: the environment is no longer the pinned one (the same version the first image installed by itself, and on which registering an account answered 500). `curl localhost:8200/admin/sentry-test` without `X-Ops-Token` → 404; with it → 500, and `curl localhost:9010/raw` shows the report with the token header `[Filtered]`. `GET /ping` with the database down → 200 and the error in the body; `GET /ready` → 503. |
| **28 (CI/CD)** | Add a column to a model and no migration (`a_column_nobody_migrated: str \| None = None` in `Trip`) → `alembic check` ends `FAILED: New upgrade operations detected: [('add_column', None, 'trips', …)]`, and so does the `backend` job. Add a test that asserts `1 + 1 == 3` → the job ends `1 failed, 1365 passed`; on `main`, the `deploy` job does not start. Delete the night's report before the summary is written (`python scripts/nightly_summary.py /tmp/an-empty-directory --require integration`) → `❌ Nightly — 1 failed`, `DID NOT RUN — no report was written`, exit 1: a night whose tests never started is not a night without failures. `python scripts/wait_for_release.py --commit 0000000000 --api http://localhost:8200 --timeout 12` against a running stack → exit 1 and what the address last answered. Take the `if: steps.target.outputs.configured == 'true'` off any step of `deploy.yml` → `test_a_repository_with_nowhere_to_deploy_to_can_still_merge` fails. Why: a pipeline's job is to say no, so each no was made to happen. |
| **29 (Observability)** | With the stack of Step 27 up: `docker stop tripplanner-observability-jaeger-1`, then plan a trip → the plan is made as before; the log has `Transient error … encountered while exporting span batch` from the exporter and nothing else changes — nothing here is in the app's path (start it again: `python scripts/observability.py up`). `curl -s -o /dev/null -w "%{http_code}" localhost:8200/metrics` on the production-like stack → `404`: a deployment's numbers are for whoever has its token. `OTEL_EXPORTER_OTLP_ENDPOINT=collector:4318 PYTHONPATH=src/backend python -m app.preflight --production` → among what stops a deployment: `✗ OTEL_EXPORTER_OTLP_ENDPOINT is not an http:// or https:// URL`, and among what it starts without: `! OPS_TOKEN is empty: GET /metrics answers nobody`. Rename a metric in `docker/observability/grafana/dashboards/tripplanner.json` (`itinerary_builds_total` → `itinerary_build_total`) → `test_every_metric_a_panel_asks_for_is_one_the_app_exports` fails and names the panel, instead of the panel going quietly empty. Open an event stream (`curl -N "localhost:8000/trips/<id>/stream?token=<your token>"`), stop it, and look at its trace in Jaeger → `http.url` ends `?token=[Filtered]`. Why: what watches a system must not be able to break it, leak from it, or go blind without anyone noticing. |
| **30 (Load test)** | With the stack of Step 28 up: in `src/ai/utils/run_logger.py` make `log_agent_run` what it was — `await db.refresh(run)` after the commit, and no `await hand_back(db)` after `_warn_if_over` — then `python scripts/loadtest.py up --small-pools` and `python scripts/loadtest.py run --rate 0.8 --name broken` → `DOES NOT HOLD: 118 of 145 visits got a plan; 26 visits could not even start; a plan took 164 s at p95; requests took 15395 ms at p99`, `database pool 15.0/15.0`, `pooler serving 8 with 11 waiting`: every plan in flight keeps a connection again. Put back only the `refresh` and it still `HOLDS` — but with `database pool 10.0/15.0` and `5 waiting` where there were 2 and none: the read that mattered more is the one after it (both runs were made on a copy for the report: `docs/loadtest/log-agent-run-as-it-was/`, `read-back-restored/`). Without the load, either way: `RUN_INTEGRATION=1 pytest tests/integration/test_phase30_connections_integration.py` fails and names the waits — `{'flights': True, 'hotels': True, 'attractions': True, 'local tips': True, ...}`. In `docker/prodlike/compose.yml` set `REDIS_MAX_CONNECTIONS: "100"` and run 3.2 visits a second → the backend's log fills with `MaxConnectionsError: Too many connections` at the 101st open page, and plans are made that nobody hears about (seen with the library's default of 100: the report, section 7). `DATABASE_URL=postgresql://u:p@db.example.com/x REDIS_URL=redis://localhost:6379 PYTHONPATH=src/backend:. python -c "import loadtest.standins.app"` → `NotTheLoadTest: DATABASE_URL is not on this machine` — the stand-ins do not start against a database that is somewhere else. Close the laptop's lid on battery during a run → `this machine was asleep for 14 s of the run: its figures are wrong, and are not kept`. Why: a load test's result is only as good as what it refuses to count. |
| **31 (Chaos)** | The app as it was before this phase is all of it broken at once, and its records are kept — `docs/chaos/before/`: the tool server killed → `✗ the next plan works … failed, searches 0 of 3, {"flight_agent": "MCP client exception: ", …}`; the builder's model silent → `✗ the trip is marked failed — planning after 901.2 s`; Redis killed → `✗ a plan asked for while Redis is away … {"http": 500, …}` and a build of 32 s. One at a time, on the tests: in `src/ai/mcp_client/client.py` take `await _forget(session)` out of `_call_tool` → `test_a_call_that_finds_the_tool_server_gone_starts_another_and_asks_again` fails (the dead session is kept). In `src/ai/builder/builder.py` take `timeout=BUILD_READ_TIMEOUT_S,` out → `test_the_builders_client_is_made_with_a_timeout_and_one_more_try` fails; make `asyncio.timeout(BUILD_DEADLINE_S)` `asyncio.timeout(None)` → the tests do not fail, they *hang* — which is the bug — and are stopped by whoever runs them. In `routes/trips.py` make the first line of `_Publisher.__call__` `if False:` → `test_a_run_does_not_wait_on_a_redis_that_is_not_there` fails (forty publishes tried, not one); make `if planned is None:` in `refine_trip` `if True:` → only the integration test `test_a_planned_trip_whose_state_redis_has_lost_can_still_be_changed` fails. In `loadtest/standins/faults.py` take the `guard.check(…)` line out → `test_a_fault_never_points_a_client_at_anything_but_this_machine` fails. Close the laptop's lid on battery during `python scripts/chaos.py all` → every record says `asleep_s` and `"timings_hold": false` (it happened: twelve hours of it). Why: a chaos exercise that passes on a broken app is worse than none. |
| **32 (Chaos, data)** | The app as it was before this phase, kept in `docs/chaos/before/`: Postgres away 45 s → `✗ … none a 500 — 23 requests: ['200', '500']` and `✗ the app answers again within 10 s … the app after 13.7 s`; killed between the two writes → `✗ the traveller is told why in their own words — the page: (sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.ConnectionDoesNotExistError'>…`; the backend killed after a save → `✗ after the restart the itinerary is embedded … {'vectors': 0, 'pending_retry': 0}, 121.8 s after the backend was ready`. And two things that were broken *by* this phase on the way, and are kept so that nobody does them again: give the driver `command_timeout=20` in `app/db/session.py` and run `python scripts/chaos.py postgres-kill --postgres-away 45` → nine requests end as 500s at the same instant, three seconds after Postgres is back (`docs/chaos/tried/`); take `await db.flush()` out of `persist_node` in `orchestrator.py` → on the stack every plan fails with a foreign-key error (the row that owes the embedding is sent before the itinerary it points at). Why: two of this phase's own fixes were wrong the first time, and only running them showed it. |
| **33 (Evals)** | In a copy, make the attractions search forget what the traveller enjoys (`activities_search_input`: `"interests": DEFAULT_INTERESTS`) and run `python scripts/run_eval.py --plan-only` there → every scenario still ends ✓ (the planner's own evaluator passes every plan — seven of its unit tests do fail on this copy), and the measurements of its records allow an average of at most 3.07 — nine plans held to 2, "the attractions search left out what the traveller enjoys" — where the healthy build's allow 4.73 (`docs/evals/broken-interests/`, `docs/evals/baseline/`). Or take the key check out of `scripts/run_eval.py` and run `pytest tests/unit/test_phase33_eval.py` → it fails at once ("a unit test must never start a backend or empty a database"). Why: an eval that only checked how plans end would pass a build whose plans are all wrong in the same way. |
| **34 (Alerts)** | In `docker/observability/alerts/rules.yml` put the cache alert's floor back as it was first written — `sum by (job, instance) (increase(mcp_cache_lookups_total[5m])) >= 10` — and run promtool (Step 32 A) → `FAILED: … the first few misses after a restart do not pass the floor …`: a backend's first four misses after a restart fire it, on the strength of the last process's lookups — what the quiet drill found (`docs/alerts/first/quiet.json`: pending at 0.21, twenty seconds after a restart). Make the error threshold `) > 0.10` → `FAILED: … an endpoint with more than 5 % of its requests failing fires …`: the endpoint at 10 % is no longer alerted on. `docker stop tripplanner_redis` with a backend running → `GET /admin/health-deep` answers 503 in under a second, `redis: ConnectionError …`, and still asks the other three; `docker start tripplanner_redis` → 200. In `src/backend/app/db/errors.py` make `_unreachable` return only `isinstance(error, OSError) and error.errno in _NO_ROUTE` → `pytest tests/unit/test_phase34_health.py -k resolve` fails; on the stack that is the app the first drill ran on, where every request with the pooler stopped was a 500 (`docs/alerts/first/errors.json`) — and the alert fired all the same, which is why it was the record, not the verdict, that showed it. Why: an alert is only as good as the condition it was proved against and the message somebody reads at night. |
| **35 (Security)** | In `src/backend/Dockerfile` put back `--forwarded-allow-ips '*'` → `pytest tests/unit/test_phase35_security.py -k believes` fails: a proxy on a private address that appended the real client, and the client is taken to be the address the client wrote itself (`6.6.6.6`) — the sign-in limit a header away, which is what the image did until this phase. Start the stack without `FORWARDED_ALLOW_IPS` and run `python scripts/security_check.py --prodlike` → `✗ an address this machine makes up is not believed … [401, 401, 401] — this machine is a trusted proxy for this backend`: from where a platform's proxy stands, it would be (Step 33 A). In `app/core/sessions.py` make the reuse check `if False:` → `RUN_INTEGRATION=1 pytest tests/integration/test_phase35_auth_integration.py -k stolen` fails: a token used twice no longer ends the sign-in. In `app/schemas/text.py` set `PASSWORD_MAX_BYTES = 10_000` → `-k bcrypt` fails: a 73-byte password is taken, and bcrypt would check it by its first 72. `curl -sI localhost:8200/no-such-route` on the stack → the seven headers, a 404 included. Why: a protection that nothing can show working is one that nobody would notice was gone. |
| **36 (Languages)** | In `src/ai/language.py` make `if script == "LATIN":` `if False:` → `pytest tests/unit/test_phase36_language.py -k latin` fails six times: "5 days in Goa, budget 50k" is read — as Afrikaans — and an English request would be sent to be translated. In `src/ai/builder/builder.py` ask every plan for Hindi (`_language_block(trip_meta.get('language') or 'hi')`) → `-k zero_change` fails: an English plan's prompt is no longer the one recorded before the phase. In `src/ai/orchestrator/orchestrator.py` make the typed-destination check in `_sync_trip` `if False:` → `RUN_INTEGRATION=1 pytest tests/integration/test_pipeline_integration.py -k devanagari` fails: a trip typed "गोवा" exports as `trip-goa-…pdf` — the run's translation written back over the traveller's own name. `python scripts/language_check.py --prodlike` → 11 passed. Why: the three ways this phase could hurt a traveller who never asked for it — an English plan changed, an English request sent to a model, a name they typed taken from them. |
