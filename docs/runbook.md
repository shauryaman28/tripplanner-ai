# On-call runbook

What to do when an alert arrives. One section per alert, in the order of the steps you will take: what it means, what to check first, what it usually turns out to be, and what fixes it. Every alert's message links to its section here (`Runbook:` at the end of the message).

The alerts are `docker/observability/alerts/rules.yml`, evaluated by Prometheus every 15 seconds and sent on by Alertmanager. Each message says which endpoint or metric, its value now against the threshold, and the counts behind it — read those first: one slow plan on a quiet evening is a different morning from every plan being slow.

| Alert | Fires when | Severity |
|---|---|---|
| [PlanLatencyHigh](#planlatencyhigh) | p99 of plans started by `POST /trips/{trip_id}/plan` over 30 s, over 10 minutes, for 5 minutes | warning |
| [EndpointErrorRateHigh](#endpointerrorratehigh) | more than 5% of one endpoint's requests answered 5xx, over 5 minutes, for 2 minutes | critical |
| [CacheHitRatioLow](#cachehitratiolow) | the tool server's cache answers under 30% of at least 10 lookups, over 5 minutes, for 3 minutes | warning |
| [ModelErrorRateHigh](#modelerrorratehigh) | more than 10 model calls a minute fail, over 5 minutes, for 2 minutes | critical |

## Where to look

| | On this machine (`python scripts/observability.py up`) | On a deployment |
|---|---|---|
| Dashboard | http://localhost:3001/d/tripplanner — the "Alerts" row at the top says what is firing | the deployment's Grafana |
| Alerts | Prometheus http://localhost:9090/alerts · Alertmanager http://localhost:9093 · what was sent: http://localhost:9011/alerts | the deployment's Alertmanager, and the channel it sends to |
| Every dependency at once | `curl -s -H "Authorization: Bearer $OPS_TOKEN" http://localhost:8200/admin/health-deep` (the production-like stack; `:8000` without a token in development) | `curl -s -H "Authorization: Bearer $OPS_TOKEN" https://<backend>/admin/health-deep` |
| Log lines | Grafana → Explore → Loki: `{job="tripplanner"}` | the platform's log explorer (JSON lines; the same fields) |
| Traces | Jaeger http://localhost:16686 — search by tag `trip.id=<id>` | the trace backend in `OTEL_EXPORTER_OTLP_ENDPOINT` |
| Errors | `python scripts/sentry_receiver.py` (stand-in, :9010) | Sentry |

`OPS_TOKEN` is the deployment's operations token; the production-like stack's is `PRODLIKE_OPS_TOKEN` in `.env.prodlike`.

### Reading GET /admin/health-deep

```json
{"status": "failing", "took_ms": 3004.2,
 "dependencies": {
   "postgres":    {"status": "failing", "required": true,  "latency_ms": 3001.0, "detail": "no answer within 3 s"},
   "redis":       {"status": "ok",      "required": true,  "latency_ms": 0.9,    "detail": "PING answered"},
   "tool_server": {"status": "ok",      "required": true,  "latency_ms": 11.8,   "detail": "estimate_budget answered: ₹27,500 for the test trip"},
   "langsmith":   {"status": "off",     "required": false, "latency_ms": null,   "detail": "no LANGCHAIN_API_KEY: traces are not sent"}}}
```

- `failing` (HTTP 503): something every plan needs is not working. `degraded` (200): only LangSmith is — plans are made, untraced. `off`: not set up, which is not a fault.
- A `latency_ms` close to the check's deadline (Postgres 3 s, Redis 2 s, tool server 10 s, LangSmith 5 s) is a dependency that is there but not answering: for Postgres, almost always the app's connection pool used up — see [EndpointErrorRateHigh](#endpointerrorratehigh).
- The tool server's first answer after a start takes a second or two longer: the check starts the process if it is not running, exactly as a plan would.
- It asks real things each time (a query, a tool call, a request to LangSmith). Poll `/ready` for liveness; ask this when you want to know why.
- It does not ask the language models — every call would spend quota. A model that is slow or failing shows in the dashboard's model panels and in [ModelErrorRateHigh](#modelerrorratehigh), not here: in this phase's drill, with the builder's model not answering and every plan failing at 36 s, the deep check said `ok` to everything, which was true.

### Silencing an alert you are already working on

Alertmanager says a firing alert again every 4 hours. To keep it quiet while you work — never to make an alert go away that nobody is working on:

```bash
docker compose -f docker/observability/compose.yml exec alertmanager \
  amtool silence add alertname=EndpointErrorRateHigh --duration=1h --comment="pooler restart in progress" \
  --alertmanager.url=http://localhost:9093
docker compose -f docker/observability/compose.yml exec alertmanager amtool silence query --alertmanager.url=http://localhost:9093
```

or Alertmanager's page → the alert → Silence. A silence expires by itself; the alert comes back if the condition is still true.

### When no alert can fire

All four are computed from the backend's own metrics. A backend that Prometheus cannot read — stopped, crashed, its `/metrics` refusing the token — is **silent**: no series, no alert. Prometheus → Status → Targets shows `down` for it. A deployment should have its platform's own health check on `/ready` (Phase 27) for exactly this.

---

## PlanLatencyHigh

### What it means

Travellers are waiting more than 30 seconds for their plan. A plan normally takes 8–25 s on the real providers (Phase 29–30). The number is the plan's own duration (`planning_run_duration_seconds{kind="plan"}`) — not `POST /plan`'s, which answers 202 in a few milliseconds and says nothing about the wait.

The message tells you how many plans there were in the 10 minutes, how many took over 30 s, and how they ended (`completed`, `no_plan`, `crashed`, `interrupted`). On this app's traffic p99 is often the slowest plan or two: **"1 plan, 1 over 30 s"** is one plan to look at, not an outage.

### Check first

1. **How they ended**, in the message. Mostly `completed` → something is slow. Mostly `no_plan` at about 36 s → something is timing out (the builder's model gives up at its deadline: Phase 31).
2. **One slow plan's trace.** Dashboard → "How long a plan takes"; then Jaeger → service `tripplanner-backend`, operation `planning run`, *Min Duration* 30s. Its children say which agent took the time: `intent_parsing`, `flight_agent` / `hotel_agent` / `activities_agent` (the tool calls under them, and the provider spans in `tripplanner-mcp`), `itinerary_builder`, `preference_extractor`.
3. **The models.** Dashboard → "Model calls by model": a red "failed (any model)" line means the slowness is retries and fallbacks — see [ModelErrorRateHigh](#modelerrorratehigh) too.
4. **The tool server.** Dashboard → "Tool call duration, p95": a provider that is slow shows here first.
5. **Under load?** Dashboard → "Plans being made", "Database connections", "Event loop lateness". Plans in progress near the pool's limit, or a late event loop, is the app itself being the bottleneck (Phase 30's report).

### Common causes

- **Gemini not answering within its 15 s deadline** (`intent_parsing` ~15.9 s, `504 DEADLINE_EXCEEDED`, then Groq's small model in under a second). Seen live in Phase 29: one plan at 26.7 s. Several in a row push the p99 over 30.
- **Groq's per-minute token limit.** Four or five plans within a few minutes on the free tier: the builder slows to ~28 s and the preference extractor falls back. `itinerary_builder` long in the trace, no error.
- **The builder's model not answering at all.** Each plan fails at about 36 s with `no_plan` (its client's own timeout, then the deadline — Phase 31). The traveller sees "planning failed".
- **A slow provider.** Duffel or LiteAPI taking 10+ s; a cold cache makes every plan wait for them (see [CacheHitRatioLow](#cachehitratiolow)).
- **The database away.** A plan in flight that loses the database waits until it is back to record that it failed, and that wait is part of its duration: in this phase's drill, a pooler stopped for 141 s put the p99 of plans at 2.9 minutes. [EndpointErrorRateHigh](#endpointerrorratehigh) fires first — start there.
- **Redis away during a plan.** The itinerary's pieces are published as they are written; each publish to a Redis that is not there waits to reconnect (Phase 31: a 7 s build took 32 s).

### Known fixes

- A provider's quota or rate limit: wait for it (Groq's resets every minute; Gemini's free tier per day at 05:30 IST), or move that agent to a paid tier / another model (`GEMINI_MODEL`, `GROQ_MODEL`, `GROQ_SMALL_MODEL`).
- Gemini timing out repeatedly: the fallback is already doing its job; if it persists, set `GEMINI_MODEL` to the model that is answering, or accept the 15 s while Google recovers.
- The builder hanging: nothing to restart — each plan ends at its deadline. Find the provider's status page; the plans work again when it answers.
- Under load: Phase 30's settings (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `REDIS_MAX_CONNECTIONS`, more instances) — the load test's report says which one runs out first.

It resolves by itself once the slow plans are older than 10 minutes.

## EndpointErrorRateHigh

### What it means

More than 5% of one endpoint's requests were answered with a 5xx over the last 5 minutes. Health checks (`/ping`, `/ready`, `/version`, `/admin/health-deep`) are not counted, and neither is `/admin/sentry-test`, which fails on purpose. The alert is per endpoint and names it; several endpoints failing at once arrive as one message listing all of them ("Every endpoint failing now: …"). 4xx is never counted: a wrong password, someone else's trip, a plan asked for twice.

On this app a 5xx is one of two things:

- **503** — "The planner cannot reach its database / cannot be reached for a moment": Postgres or Redis is away (Phases 31–32). Nothing is lost; the client is told to try again.
- **500** — "Something went wrong on our side. It has been reported.": a bug. It is in Sentry, with its stack trace.

The message gives the count behind the percentage: "50 requests, 5 of them 5xx".

### Check first

1. `GET /admin/health-deep`. `postgres` or `redis` failing → it is a 503 incident; go to step 3. All `ok` → probably a 500; step 2.
2. **Sentry** (or the stand-in's `/events`): the newest error on that endpoint, and its stack trace. Its `release` says which commit is running.
3. **The log lines.** Explore → Loki: `{job="tripplanner", level="warning"} |= "could not be served"` — each says whether it was the database or Redis. `{job="tripplanner", level="error"}` for the 500s.
4. **Postgres or the pooler.** `docker ps` on this machine (`tripplanner-prodlike-pooler-1`, `…-neon-standin-1`); on a deployment, the database provider's status page and its connection count.
5. **The app's own pool.** Dashboard → "Database connections": in use at the limit for minutes → requests are waiting for a connection, and time out (the deep check's Postgres then takes ~3 s).

### Common causes

- **The database cannot be reached**: Postgres restarted, the pooler down, the provider's maintenance window. Every endpoint that needs the database fails together — `POST /trips`, `GET /trips`, `/status`, `/auth/login` — and arrives as one message listing them. (This phase's drill stops the pooler: `GET /trips`, `POST /trips` and `GET /trips/{trip_id}/status` in one message, about two and a half minutes after it stopped.)
- **The database's host name does not resolve**: the pooler's container gone, a DNS outage, a hosted database's address changed. The log says `gaierror: [Errno -2] Name or service not known`; the deep check's `postgres` detail says the same. Answered 503 like any other way of the database being away (until Phase 34 it was a 500 — DECISIONS #311).
- **Redis cannot be reached**: `POST /plan`, `/refine`, the event streams fail with 503; reading trips still works. [CacheHitRatioLow](#cachehitratiolow) usually fires with it.
- **The app's connection pool used up** under load (Phase 30): requests wait, then time out with a 500.
- **A bug** on one endpoint: one endpoint failing alone, everything in health-deep `ok`, the same error in Sentry over and over — usually right after a deploy.

### Known fixes

- The database away: bring it (or the pooler) back; the app reconnects by itself (`pool_pre_ping`, Phase 32) — no restart needed. Start-up recovery re-queues what was owed.
- Redis away: bring it back; event streams reconnect by themselves within ~30 s (the page's backoff, Phase 31).
- Pool exhaustion: Phase 30's `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, within what the hosted pooler lends (Neon: 90% of `max_connections`), or another instance.
- A bug after a deploy: roll back to the previous release (Railway → Deployments → the previous one → Redeploy), then fix forward.

It resolves 5 minutes after the errors stop (the window), plus Alertmanager's minute.

## CacheHitRatioLow

### What it means

The tool server's cache answered fewer than 30% of its lookups over the last 5 minutes (of at least 10 lookups in that process — a quiet hour, or the first trip after a restart, cannot fire it). Normally each search is a **miss** when a trip is created (cache warming asks the provider) and a **hit** when the trip is planned: about 0.5. Under 0.3, plans are asking Duffel, LiteAPI and OpenTripMap themselves — slower plans, and the providers' rate limits spent faster.

The message breaks the lookups down by cache and result:

- **error** — Redis could not be asked at all.
- **miss** — Redis answered, and the entry was not there: the warming did not run, the entries were deleted or evicted, or the key a plan asks for is not the one that was warmed.

### Check first

1. **The breakdown in the message.** Mostly `error` → Redis; mostly `miss` → the warming or the entries.
2. `GET /admin/health-deep`: `redis` failing settles it.
3. **The warming's own lines.** Explore → Loki: `{job="tripplanner"} |= "[CACHE WARM]"` — every trip created should have one per search ("3 result(s) ready in 5.4 s"). None at all → warming is switched off (`CACHE_WARMING_ENABLED`) or trips are being created with a free-text destination (not warmed by design).
4. **Redis's memory.** `INFO memory` and `INFO stats` (`evicted_keys`) — a hosted Redis at its memory limit evicts entries as fast as they are written.
5. Dashboard → "Cache hits by cache": one cache alone falling (say `flights`) points at that search's key, not at Redis.

### Common causes

- **Redis away** — every lookup an `error`, and [EndpointErrorRateHigh](#endpointerrorratehigh) fires for `POST /plan` beside it.
- **Entries gone before they are read**: Redis evicting under memory pressure, a `FLUSHDB` by hand, a TTL far shorter than the time between creating and planning a trip. (This phase's drill deletes `mcp:*` every half second: every lookup became a miss at once, the five-minute ratio fell under 0.3 in two and a half minutes as the misses outnumbered the hits before them, and the alert fired three minutes later.)
- **Warming not running**: `CACHE_WARMING_ENABLED=false`, or trips created with a sentence for a destination.
- **Plans asking for different searches than were warmed**: a request that names another origin ("from Mumbai") — a miss by design, rare enough not to fire this.

### Known fixes

- Redis away: bring it back — the cache refills itself as trips are created.
- Evictions: a larger Redis plan. Changing the eviction policy does not help: the conversations a refinement needs expire too (a day), so they are as evictable as the cache.
- Warming off: switch `CACHE_WARMING_ENABLED` back on.

It resolves once the window holds enough hits again — a few minutes of normal trips.

## ModelErrorRateHigh

### What it means

More than 10 calls to a language model a minute are failing (over 5 minutes). A failure is a call the provider's client gave up on, after its own retries: a 429 (quota or rate limit), a 500, a timeout. The message says which agents and models are failing and what share of all calls that is.

Most agents have a way round a failed call — the short prompts (intent parsing, the refinement classifier) go from Gemini to Groq's small model; the preference extractor falls back to a rule for the travel style and learns nothing else from that trip — so **plans may still be made while this fires**. The itinerary builder has no fallback: if `itinerary_builder` is in the list, plans are failing.

### Check first

1. **Which agent → model**, in the message. Gemini only (`intent_parsing → gemini-…`) is the fallback working; both Gemini and `openai/gpt-oss-20b` means the short prompts have nowhere left to go; `itinerary_builder` means no plans.
2. **The log lines.** Explore → Loki: `{job="tripplanner"} |= "Gemini unavailable"` (the fallback being taken, with the error), and `{job="tripplanner", level="warning"} |= "429"`.
3. **The provider's status code** in a failed model span (Jaeger → `llm <model>`, marked as an error, its message). `429 RESOURCE_EXHAUSTED … Quota exceeded for metric … limit: 20` is Gemini's free tier for the day; `rate_limit_exceeded` with `Retry-After` is Groq's minute.
4. Dashboard → "Model calls by model" and "Tokens": a jump in tokens a minute before the errors is the app spending its own quota (a burst of plans, a retry loop).

### Common causes

- **Gemini's free tier spent**: 20 `generate_content` requests a day per project per model. Every intent parse fails over to Groq; the alert fires once traffic is above ~10 plans a minute.
- **Groq's per-minute limit**: 8,000 tokens a minute per model on the free tier, and one plan uses most of it. A burst of plans → 429s on `gpt-oss-120b` (builder, preferences) and `gpt-oss-20b` (the fallback).
- **A provider outage**: 500s or timeouts from one provider, all its models at once.
- **A key revoked or rotated**: every call to that provider failing with 401/403 — check that the key in the deployment's settings is the current one.

### Known fixes

- Quota: wait for the reset (Groq: the minute; Gemini: 05:30 IST), or move to a paid tier; `GEMINI_MODEL=gemini-3.5-flash-lite` has a quota of its own.
- A burst: nothing to fix if it was real traffic; if it was a loop (the same trip retried over and over), find it in the log by its `trip_id`.
- An outage: the provider's status page; the fallbacks carry the short prompts; there is no fallback for the builder.
- A key: put the current one in the deployment's settings and redeploy.

It resolves once the failures fall under 10 a minute over 5 minutes — a few minutes after they stop.
