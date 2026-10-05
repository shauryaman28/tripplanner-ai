# Phase 24 — Caching & Rate Limit Handling

**Status: ✅ Complete**
**Done criterion:** Differentiated TTLs verified via Redis CLI. Rate limit scenario handled gracefully. Backoff tested with a forced 429.

Decisions: `DECISIONS.md` #151–#161. Three things are worth knowing before reading on, because none of
them is in the roadmap's entry. The MCP tools had been running one at a time: a plan's "parallel" hotel
and attractions searches were answered one after the other (#155). A flight or hotel search was cached
under its budget, which the provider is never told — so the same search was repeated on every re-plan,
and hotels could not be warmed at all (#156). And the acceptance line "visible in logs" was false for
every INFO line the app had ever written (#160).

## What was built

```
src/ai/mcp_server/rate_limiter.py      ← NEW: each provider's limit; a sliding window that queues the request over it
src/ai/mcp_server/outbound.py          ← NEW: send() — the one way a request leaves: in its turn, retried with backoff
src/ai/mcp_server/tools.py             ← every request through send(); flights and hotels cached by what the provider
                                         is asked; one search per cache key at a time; RATE_LIMITED
src/ai/mcp_server/cache.py             ← single_flight(key); the Redis connection made once, whatever the threads
src/ai/mcp_server/server.py            ← the tools run in worker threads
src/ai/orchestrator/warming.py         ← NEW: a trip's opening searches, made while it is being created
src/ai/orchestrator/orchestrator.py    ← what each search agent is given, as functions warming shares
src/ai/agents/{flight,hotel,activities}_agent.py ← the tool call each makes, as a function warming shares
src/ai/utils/failures.py               ← "This search is busy right now. Try again in a minute."
src/backend/app/api/routes/trips.py    ← POST /trips starts warming
src/backend/app/core/config.py         ← CACHE_WARMING_ENABLED
src/backend/app/main.py                ← the app's INFO log lines are shown; version 0.24.0
scripts/cache_ttls.py                  ← NEW: every cached key's TTL against the Phase 3 spec

tests/fakes.py                         ← FakeProviders, memory_cache, tool_server: the real tools behind fake providers
tests/conftest.py                      ← no test waits for a limit or a backoff, or warms a cache, unless it means to
tests/e2e/stub_backend.py              ← Munnar: the flight provider says "asked too often" until retried
```

## The request path, before and after

```
before   agent → call_tool → MCP server ─ one tool at a time ─→ cache (key: the tool's whole input) → httpx → provider
                                                                                                      └ any error: ToolError

after    agent → call_tool → MCP server ─ a thread per call ─→ single_flight(key) → cache (key: what the provider is asked)
                                                                   └ miss → send(): rate limit's turn → httpx → provider
                                                                                └ 429 / 5xx / no connection: wait, try again (×4)
         POST /trips ─ background ─→ the same call_tool, the same parameters ─→ the cache is filled before planning asks
```

## Dev A — cache warming and rate limits

### The limits (#151)

Each was read from the provider that is actually called. The probes, one request each:

| Provider | Request | What it said | Limit kept |
|---|---|---|---|
| Duffel | `POST /air/offer_requests` | `ratelimit-limit: 30`, `ratelimit-reset` on the next minute | 30 / 60 s |
| | `GET /air/airlines` | `ratelimit-limit: 4000` — the limit is per endpoint | |
| LiteAPI | `POST /hotels/rates` | `x-ratelimit-limit: 5` (per second) | 5 / 1 s |
| OpenTripMap | `GET /places/radius` | `x-ratelimit-limit: 10` (per second) | 10 / 1 s |
| OpenWeatherMap | `GET /data/2.5/forecast` | no rate-limit headers; the free plan documents 60 a minute | 60 / 60 s |
| Nominatim | `GET /search` | no headers; the usage policy says 1 a second | 1 / 1 s |

The roadmap's numbers (Amadeus 60/min, Google Maps 100/min) are for providers this app no longer calls.
Each window is kept 10% longer than the provider's (`HEADROOM`): 66 s, 1.1 s.

### The limiter (#152, #153)

`RateLimiter.acquire()` reserves the earliest moment that keeps the limit and sleeps until then. Callers
are served in the order they asked. A wait longer than 60 s is refused (`QueueFull` → `RATE_LIMITED`).
It lives in the MCP server, where the requests are sent, not in the client the roadmap names — the client
cannot tell a cache hit from three requests.

On the real geocoder — three places nobody had looked up, at once:

```
[RATE LIMIT] nominatim: limit of 1 per 1.1 s reached — waiting 1.10 s
[RATE LIMIT] nominatim: limit of 1 per 1.1 s reached — waiting 2.20 s
  Bundi      answered after 1.89 s (2 results)
  Orchha     answered after 2.30 s (5 results)
  Mandu      answered after 4.32 s (NO_RESULTS)
```

No 429, no backoff: the requests were spaced, not turned away.

### Three agents at once (the roadmap's test)

`test_three_agents_searching_at_once_never_see_a_429` runs three real `FlightAgent`s through the real
client and the real FastMCP server against a fake Duffel that counts in real time and answers 429 to a
third request inside its window. The limiter is given the same limit and really waits. Result: three
agents with flights, three answers of 200, zero 429s, no backoff, and one logged wait. A second test
forces a 429 the limiter could not have prevented: it is caught, retried after 1–2 s, and all three
agents complete.

### Cache warming (#158)

`POST /trips` → `start_cache_warming(trip, user)` → a background task → `warm_trip_caches`. On the real stack:

```
POST /trips → 201 in 31 ms
INFO:     [CACHE WARM] weather: Jodhpur, 2026-11-19 to 2026-11-22 — 4 result(s) ready in 0.2 s
INFO:     [CACHE WARM] hotels: Jodhpur, 2026-11-19 to 2026-11-22, 2 guest(s) — 5 result(s) ready in 1.9 s
INFO:     [CACHE WARM] flights: DEL → Jodhpur on 2026-11-19, back 2026-11-22 — 1 result(s) ready in 2.0 s
INFO:     [CACHE WARM] attractions: Jodhpur: history, food — 5 result(s) ready in 2.9 s
```

Then the plan of that trip:

```
  intent_parsing            completed     1605 ms
  flight_agent              completed       21 ms   ← [CACHE HIT] mcp:flights:…
  hotel_agent               completed       79 ms   ← [CACHE HIT] mcp:hotels:…
  activities_agent          completed       77 ms   ← [CACHE HIT] mcp:attractions:v2:…
  destination_intelligence  completed     1684 ms
  itinerary_builder         completed     4843 ms
trip completed after 8.3 s
```

The three searches of a plan took 21–79 ms. On the Phase 22 live run the hotel and attractions searches took 5.2 s.

| What is warmed | When | With |
|---|---|---|
| flights | always | origin: the saved home city, else Delhi; the trip's dates, travellers, one stop — planning's first search |
| hotels | always | the place, dates and guests (the nightly budget is no longer in the key, #156) |
| weather | always | the trip's dates. Nothing reads it before Phase 38; more than five days out it costs no request |
| attractions | the trip names interests | those interests plus the saved dietary restrictions, as planning merges them |
| nothing | the destination is a sentence | planning has to read it first |

## Dev B — backoff and TTLs

### Backoff (#154)

`outbound.send(provider, request)` wraps every provider request: `wait_exponential_jitter(initial=1, max=30)`,
`stop_after_attempt(4)`.

| Answer | Tried again? |
|---|---|
| 429 | yes — and `Retry-After` / `ratelimit-reset` is the wait when it is longer than the backoff, up to 30 s |
| 500, 502, 503, 504 | yes |
| the connection was never made | yes |
| 400, 401, 403, 404, 422 | no: a second try cannot change it |
| read timeout | no: the provider had the request and its full time |

The forced 429 (`test_a_429_is_tried_again_after_waits_that_roughly_double`): the provider answers 429,
429, 429, 200. The three waits are asserted to lie in 1–2 s, 2–3 s and 4–5 s, and the same three numbers
are parsed back out of the log:

```
[BACKOFF] duffel: HTTP 429 — waiting 1.30 s before attempt 2 of 4
[BACKOFF] duffel: HTTP 429 — waiting 2.85 s before attempt 3 of 4
[BACKOFF] duffel: HTTP 429 — waiting 4.45 s before attempt 4 of 4
```

Four 429s in a row end as `ToolError(code="RATE_LIMITED")`; on the trip page that reads "This search is
busy right now. Try again in a minute." with a Retry button (Playwright: Munnar).

### TTLs against the Phase 3 spec (#159)

| Cache | Spec (Phase 3) | Constant | Read from Redis, 20 s after writing (live) | Discrepancy |
|---|---|---|---|---|
| `mcp:flights:*` | 5 min | `TTL_FLIGHTS = 300` | 280 s | none |
| `mcp:hotels:*` | 15 min | `TTL_HOTELS = 900` | 880 s | none |
| `mcp:attractions:v2:*` | 6 hr | `TTL_ATTRACTIONS = 21_600` | 21,581 s | none |
| `mcp:weather:*` | 1 hr | `TTL_WEATHER = 3_600` | 3,579 s | none |
| `mcp:geocode:v2:*` | — | `TTL_GEOCODE = 2_592_000` | 2,591,980 s | an addition (#72), not in the spec |

Nothing had to be corrected. Three things were found on the way and are written down so they are not
mistaken for discrepancies later:

- `redis-cli TTL 'mcp:flights:*'` — the roadmap's command — answers `-2` whatever is cached: `TTL` takes
  a key, not a pattern. Use `python scripts/cache_ttls.py`, or the loop in `HOW_TO_RUN.md` Step 22.
- A climate estimate (weather more than five days out) is cached for the same hour as a forecast.
- Only answers are cached. An error, and an answer with nothing in it, are asked for again next time.

```
$ python scripts/cache_ttls.py
cache         keys   expires in               spec
flights          1   280 s                    300 s          ok
hotels           1   880 s                    900 s          ok
attractions      2   9,750–21,581 s           21,600 s       ok
weather          1   3,579 s                  3,600 s        ok
geocode         14   2,346,213–2,591,980 s    2,592,000 s    ok   (not in the Phase 3 spec)
```

## Not in the roadmap, and needed for it

### The tools ran one at a time (#155)

Three searches at once for the same city, cold cache, real providers, through the real MCP subprocess:

| | flights | hotels | attractions | all three done |
|---|---|---|---|---|
| before (`main`), run 1 | 5.74 s | 7.39 s | 7.40 s | 7.40 s |
| before, run 2 | 2.13 s | 3.93 s | 3.94 s | 3.94 s |
| after, run 1 | 2.27 s | 0.80 s | 1.83 s | 2.27 s |
| after, run 2 | 1.23 s | 0.82 s | 1.61 s | 1.61 s |

Before, each answer waits for the ones queued ahead of it, and the last two leave together. After, each
leaves when it is done. A queued or backing-off request would otherwise have frozen every other tool.

### The cache key (#156)

| | key before | key now | applied after the cache is read |
|---|---|---|---|
| flights | origin, destination, date, return date, **budget**, passengers, stops, **preferred airlines** | airports, dates, passengers, stops | budget, preferred airlines |
| hotels | destination, dates, **nightly budget**, guests | place, dates, guests | nightly budget |

## Found and fixed along the way

| Problem | Fix |
|---|---|
| A plan's hotel and attractions searches were made one after the other, and answered together | The tools run in threads (#155) |
| The same flight search under another budget, or the same stay on a re-plan, went to the provider again | Cached by what the provider is asked (#156) |
| Two identical searches started together both went out | One search per key at a time (#157) |
| The geocoder's lock was process-wide: looking up one place held up every other | A lock per place; the 1-a-second rule moved to the rate limit (#157) |
| The roadmap's `TTL mcp:flights:*` cannot fail: it answers `-2` for any pattern | `scripts/cache_ttls.py` (#159) |
| No INFO line the app logged was ever displayed | `_show_app_logs()` (#160) |
| With the tools in threads, three searches arriving together each opened a Redis connection | The connection is made under a lock |
| The first thread test claimed to catch a limiter without its lock, and did not — found by removing the lock | A test that holds one caller inside and checks the second waits |
| `tools.py`'s header said its TTLs "match Phase 24 spec (implemented early)" | They are Phase 3's; corrected |

## Tests

```
pytest tests/unit tests/contract                 778 unit + 8 contract = 786 (90 new)
RUN_INTEGRATION=1 pytest tests/integration       39 (3 new)
npx playwright test                              56 (1 new)
```

`tests/unit/test_phase24_rate_limits.py` — the limiter on a clock the test owns and on real threads; which
answers `send()` tries again, the waits, the log; a forced 429 from the flight provider; every provider's
429 named in a `RATE_LIMITED` error; the tool schemas unchanged by threading; two searches with their
providers at the same moment; three agents at once with zero 429s.

`tests/unit/test_phase24_caching.py` — what a search is cached under; the five cheapest are enough;
empty answers are not kept; every TTL; two identical searches ask once; warming's searches, its log
lines, its preferences, what it skips; a plan after warming asks no provider anything; a plan that starts
during warming waits for it; `POST /trips` starts it and the switch stops it; the log lines are shown.

`tests/integration/test_phase24_caching_integration.py` — the five TTLs read back from a real Redis and
through `scripts/cache_ttls.py`; a key that never expires is reported; and the whole flow over HTTP:
`POST /trips` answers while every provider is still held, the caches fill, `POST /plan` completes with
three cache hits, no miss, and not one more request to any provider.

`src/frontend/e2e/polish.spec.ts` — a search turned away for being asked too often says so in the
traveller's words, survives a reload, and Retry finds the flights.

### Break it on purpose

Forty-two breaks, each applied to a copy of the repo, one at a time, and the Phase 24 tests run against it. All are
caught: forty-one by a test that fails, one by tests that hang. (The first pass caught forty-one: a limiter
without its lock got through, and the test that now catches it was written because of that.)

| Break | Caught by |
|---|---|
| A request no longer takes its turn in the rate limit | every attempt takes its turn; three agents at once |
| The limiter forgets what it let through / never thinks the window full | the request over the limit waits |
| The limiter has no lock | one caller at a time decides its turn |
| A queue of any length is joined | a queue too long to join is refused |
| No headroom over the provider's window | a provider has one limiter with a window a little longer |
| A 429 is not retried / a 401 is / a read timeout is | which answers are tried again |
| The wait does not grow / has no jitter / five attempts | waits that roughly double; jittered; given up on after four |
| `Retry-After` ignored / not capped | a provider that says how long to stay away |
| A 429 that outlasts the attempts keeps the old error code | given up on → `RATE_LIMITED` |
| The log line carries the error's text (and so the key) | a wait in the log never carries the request |
| The tools run on the event loop again | two searches with their providers at the same moment |
| The flight key includes the budget / ignores the stops | another budget is a cache hit; asked differently is another search |
| The hotel key includes the nightly budget | the same stay under another nightly budget |
| The budget is not applied to cached flights / an empty answer is kept | cache hit under another budget; an empty answer is not kept |
| Two identical searches (or place lookups) both go out | two identical searches at once ask once |
| Every search waits for every other | different searches do not wait; two searches with their providers at the same moment |
| A failed search keeps its key held | the tests hang, and are stopped — the next search with that key never gets in |
| The cache connects once per thread | searches that start together share one connection |
| Flights kept ten minutes / weather a day | every cache is kept as long as the spec says |
| The cache is written with no expiry / hotels kept an hour | the TTLs read back from Redis (integration) |
| Warming: attractions without interests / ignores preferences / a re-plan's flights / a sentence as a place | warming's own tests; a plan finds everything warming cached |
| Warming or its background task lets an error out | never raises; ends in a log line |
| Creating a trip warms nothing / the switch is ignored | creating a trip starts warming; warming can be switched off |
| `RATE_LIMITED` has no words / cannot be retried | a rate-limited search, in the traveller's words |
| The app's INFO lines are not shown | the log lines are there to be seen |

## Done criterion checklist

- [x] Cache warming fires on `POST /trips`, visible in the logs as `[CACHE WARM] flights: …`
- [x] A rate limiter per provider; a request over the limit is queued and waits rather than failing
- [x] Three concurrent agents on one provider: the excess is serialised, zero raw 429s, all three complete
- [x] A forced 429 is caught, retried after backoff, and the search still answers
- [x] `tenacity` backoff with jitter around every external API request: `wait_exponential_jitter(initial=1, max=30)`, `stop_after_attempt(4)`
- [x] Backoff waits roughly double, with jitter — asserted on the logged wait times
- [x] All four TTLs read back from Redis and checked against the Phase 3 spec; no discrepancy; the fifth documented
- [x] Verified on the real stack: warming, the TTLs, a warmed plan, the limiter queueing real requests
- [x] Break-on-purpose checks: every one caught

## Known limits

- **The limits are counted per process.** One backend process runs one MCP server, and that is how the app
  is run (`docker-compose.yml`: one uvicorn worker). Several workers would each allow the full limit; the
  windows would then have to live in Redis (#152).
- **A provider counts arrivals; the limiter counts departures.** The 10% headroom covers ordinary network
  jitter. A request delayed in transit by more than that can still be answered 429 — which is what the
  backoff is then for.
- **A real provider's 429 was not provoked.** Doing so means sending a provider more than it allows, on
  purpose, with a shared test token. The 429 handling is tested against fakes that answer exactly as the
  providers' documentation describes; the limiter was verified live, by the absence of 429s.
- **Warming spends a flight search on every trip created**, planned or not — one of Duffel's thirty a
  minute. A warm-up does not step aside for a traveller's own search when the window is nearly full.
  `CACHE_WARMING_ENABLED=false` turns it off.
- **A warmed flight search is only good for five minutes.** A traveller who creates a trip and plans it
  later than that searches again; so does a plan whose request names another origin, or interests the
  trip did not have.
- **Weather is warmed and not yet read** (Phase 38). When it is, its caller must ask with the same
  parameters (`{destination, "start to end"}`) or the warmed entry is a miss.
- **A queued request holds a worker thread** (40 by default) for up to 60 s. Forty searches waiting on
  one provider would stall the others; the app is nowhere near that.
- **The LLM and embedding calls are not behind this limiter.** They have their own handling (#69, Phase 14),
  and their limits are per day or per token, not per second.
