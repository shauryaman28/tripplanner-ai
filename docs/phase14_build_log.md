# Phase 14 — Embedding Generation: OpenAI text-embedding-3-small

**Status: ✅ Complete**
**Done criterion:** Two `embeddings` rows written per itinerary (full-text + structured summary). `embedding_model="text-embedding-3-small"`, `len(vector)=1536` for both rows. Graceful fallback to `pending_retry` row on OpenAI failure. Startup recovery re-queues pending rows automatically. 21 unit tests pass, zero network calls.

## What was built

```
src/ai/embeddings/
├── __init__.py         ← public re-export of embedder API
└── embedder.py         ← build_full_text, build_summary_text,
                           _call_openai_embed, write_embedding_rows

src/ai/utils/
└── embeddings.py       ← generate_embeddings() (orchestrator seam, Phase 14 rewrite)

src/backend/app/
└── main.py             ← _recover_after_restart() in lifespan

requirements.txt        ← openai>=1.0.0, tenacity>=8.0.0 added

tests/unit/
└── test_phase14_embeddings.py   ← 21 unit tests

tests/integration/
└── test_phase14_integration.py  ← 4 integration tests (RUN_INTEGRATION=1)

docs/
└── phase14_build_log.md
```

## Architecture

### Two rows per itinerary

| Row | Text type | Purpose |
|---|---|---|
| 1 | Full-text | Dense: every activity + hotel concatenated. Best for "find an itinerary similar to this entire trip". |
| 2 | Structured summary | Compact: `"{destination} N days M INR {budget_range}. Top activities: a, b, c."` Best for Phase 23 similarity search — encodes trip profile signal more tightly. |

### Network seam

`_call_openai_embed(text)` is the **only** function that touches the OpenAI API. It is decorated with `tenacity.retry` (exponential back-off + jitter, 4 attempts). Tests patch this seam — the pattern is identical to how `test_mcp_client.py` patches the Amadeus client.

### Failure handling

```
_call_openai_embed raises after 4 retries
        ↓
write_embedding_rows catches, calls db.rollback()
        ↓
Writes ONE pending_retry row (vector=NULL)
        ↓
db.commit() → trip stays COMPLETED
```

On next app startup, `_recover_after_restart()` in `main.py` queries for `pending_retry` rows and schedules `generate_embeddings()` as background asyncio tasks.

### Idempotency

`write_embedding_rows` deletes any pre-existing `pending_retry` rows for the itinerary before writing fresh ones. This means startup recovery can call `generate_embeddings()` multiple times without producing duplicate rows.

## Text builder details

### `build_full_text`
- Iterates all days → all slots (morning/afternoon/evening) → extracts `activity` name
- Appends hotel name per day
- Includes the `"Explore the area"` fallback phrase (it's real content, unlike the summary which excludes it)
- Empty itinerary → `"No itinerary data available."`

### `build_summary_text`
- Excludes `"Explore the area"` — it's a builder fallback, not a real activity
- Budget label: `< ₹30k` → budget / `₹30–80k` → mid-range / `> ₹80k` → luxury
- Top 5 unique activities (deduped across days)
- `None` total_cost → shows `"unknown"`

## Startup recovery (`main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await init_redis()
    await redis.delete(EMBEDDINGS_PENDING_KEY)  # no job survives a restart
    await _recover_after_restart()  # re-queues pending_retry rows
    yield
    ...
```

`_recover_after_restart()` swallows all exceptions so a DB connectivity issue on startup never prevents the app from booting. Re-queued jobs are started with `spawn()` (a strong reference is kept, so the task cannot be garbage-collected mid-flight).

## Background execution + health endpoint (added in the Phase 1–17 audit)

- `persist_node` no longer awaits `generate_embeddings()` — it spawns it. `planning_complete` used to wait for the OpenAI call and its retry back-off (≈7 s on every trip without an `OPENAI_API_KEY`).
- `generate_embeddings()` increments the Redis counter `embeddings_pending` while it runs.
- `GET /admin/embedding-health` → `{"status": "ok" | "degraded", "in_flight": <counter>, "pending_retry": <rows awaiting retry>}`.
- Bug fixed: stale `pending_retry` rows were never removed because `AsyncSession.delete()` was not awaited.

## Done criterion checklist

- [x] Two `embeddings` rows written per itinerary (full-text + summary)
- [x] `embedding_model = "text-embedding-3-small"` for both rows
- [x] `len(vector) = 1536` for both rows
- [x] `_call_openai_embed` is the only network seam (patched in all tests)
- [x] tenacity retry: exponential back-off, 4 attempts, jitter
- [x] OpenAI failure → `pending_retry` row, trip stays COMPLETED
- [x] Startup recovery re-queues `pending_retry` rows as asyncio tasks
- [x] Stale `pending_retry` rows cleaned up before writing fresh ones (idempotent)
- [x] `generate_embeddings()` opens its own `AsyncSessionLocal`
- [x] `generate_embeddings()` never propagates exceptions to caller
- [x] 21 unit tests pass, zero network calls
- [x] 4 integration tests (skip unless `RUN_INTEGRATION=1`)
- [x] `openai>=1.0.0` and `tenacity>=8.0.0` added to `requirements.txt`
- [x] DECISIONS.md updated (entries 29–31, 53)
- [x] `embeddings_pending` counter + `GET /admin/embedding-health`
- [x] Embedding generation never delays `planning_complete`
