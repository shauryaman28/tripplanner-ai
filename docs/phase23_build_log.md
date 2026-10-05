# Phase 23 — pgvector Similarity Search

**Status: ✅ Complete**
**Done criterion:** `GET /trips/{id}/similar` returns sensibly related trips. Embedding strategy documented and justified. (It had answered 501 since Phase 5.)

Decisions: `DECISIONS.md` #140–#150. Two things are worth knowing before reading on. The roadmap expects
the summary Phase 14 stored to give the best similarity; measured, it gave the worst, and put a Goa beach
trip nearer a Ladakh trek than the Andaman beaches — so the summary was rewritten and every stored vector
made again (#140, #145). And "similar past itineraries" are the traveller's own, never another
account's (#143).

## What was built

```
src/backend/app/search.py              ← NEW: nearest trips by cosine similarity; similar trips; search; the cut-offs
src/backend/app/api/routes/trips.py    ← GET /trips/{id}/similar (was 501); GET /trips/search?q=…
src/backend/app/schemas/trip.py        ← TripMatch, SimilarTripsResponse, TripSearchResponse
src/ai/embeddings/embedder.py          ← the new summary text; `kind` on each row; document / query task types;
                                         embed_query (one attempt, for a waiting request)
src/backend/app/models/embedding.py    ← kind; the HNSW index over summaries only
migrations/versions/004_embedding_kind.py ← kind, the partial index, and every trip's embeddings queued again
src/backend/app/main.py                ← startup recovery embeds one itinerary at a time; version 0.23.0
scripts/embedding_experiment.py        ← NEW: the experiment — every number below comes out of it
scripts/seed_demo_trips.py             ← NEW: a demo account with twelve embedded trips

src/frontend/src/components/SimilarTrips.tsx ← NEW: three cards under the map
src/frontend/src/app/trips/page.tsx    ← the search box, and the trips it finds
src/frontend/src/components/ItineraryView.tsx, lib/api.ts, lib/types.ts

tests/fakes.py, tests/conftest.py, tests/e2e/stub_backend.py ← a stand-in embedder; no real embedding call in
                                         any test; Leh, a trip that is like no other
```

## The experiment

```
EMBEDDING_EXPERIMENT_CACHE=/tmp/vectors.json python scripts/embedding_experiment.py
```

Twelve itineraries — four beach, two mountain, three heritage, two spiritual, one nature — built with the
application's own text functions and embedded by the real model. For each text, two questions: are a
trip's three nearest neighbours of its own kind (22 are possible), and are a query's three nearest trips
of the kind it asks for (14 are possible, over six queries)?

| Text an itinerary is embedded as | task type | same kind in a trip's top 3 | Goa: Andamans − Ladakh | right in a query's top 3 |
|---|---|---|---|---|
| full text | none | 17 / 22 | +0.056 | 13 / 14 |
| | RETRIEVAL_DOCUMENT | 17 / 22 | +0.029 | 13 / 14 |
| | SEMANTIC_SIMILARITY | 21 / 22 | +0.036 | 14 / 14 |
| Phase 14's summary | none | 17 / 22 | **−0.021** | 13 / 14 |
| | RETRIEVAL_DOCUMENT | 17 / 22 | **−0.018** | 14 / 14 |
| | SEMANTIC_SIMILARITY | 18 / 22 | +0.003 | 14 / 14 |
| new summary + length and cost | none | 20 / 22 | +0.035 | 13 / 14 |
| | RETRIEVAL_DOCUMENT | 22 / 22 | +0.028 | 13 / 14 |
| | SEMANTIC_SIMILARITY | 21 / 22 | +0.034 | 14 / 14 |
| **new summary** | none | 20 / 22 | +0.057 | 14 / 14 |
| | **RETRIEVAL_DOCUMENT** ← used | **22 / 22** | +0.028 | **14 / 14** |
| | SEMANTIC_SIMILARITY | 22 / 22 | +0.054 | 14 / 14 |

The third column is the roadmap's own case: how much nearer a Goa beach trip is to the Andaman beaches
than to a Ladakh trek. With Phase 14's summary it is negative — the trek is nearer.

```
Phase 14:  Goa 4 days 42,000 INR mid-range. Top activities: Baga Beach, Calangute Beach, Fort Aguada, …
now:       beach and food trip to Goa. Kinds of places: beach, history, spiritual. Places: Baga Beach, Calangute Beach, …
```

**Why.** The old text opens with a length, a cost and a budget word, and that is what two trips then
share: Goa and Ladakh were both "mid-range", the Andamans "luxury". The new one opens with what the trip
is for and says nothing of its size.

**The cut-offs**, from the same run with the text and task types now used:

```
Two stored trips of the same kind  (11 pairs): 0.850–0.906
Two stored trips of different kinds (55 pairs): 0.782–0.859
  at 0.84: 11/11 of the same kind kept, 4/55 of different kinds let in        → SIMILAR_FLOOR = 0.84

Best match of a real query (13): 0.614–0.730; of a nonsense one (3): 0.478–0.554   → SEARCH_FLOOR = 0.58
  trips within 0.04 of the best match: 30 right, 4 wrong, 2 missed             → SEARCH_WINDOW = 0.04
```

## Dev A — similar trips

`GET /trips/{id}/similar` → up to five of the traveller's other trips, most alike first:

```json
{"status": "ready", "results": [
  {"trip": {"id": "…", "destination": "South Goa", "start_date": "2027-01-10", "…": "…"},
   "itinerary_id": "…", "total_cost": 35000.0, "highlight": "Palolem Beach", "similarity": 0.9057}
]}
```

- compared by the summary embedding of each trip's **latest** itinerary — one result per trip (#144);
- only the caller's **own** trips (#143);
- only trips **above 0.84**: the nearest trip is not "similar" for being nearest (#141);
- `"status": "pending"` while this trip's own embedding is still being made (#147); `404` for a trip that
  is not the caller's, or has no itinerary.

**HNSW or IVFFlat** — HNSW, over the summaries only; why, and when the planner uses it: #142.

## Dev B — on the page, and search

**Similar trips** — under the map, three cards: destination, dates, what the plan came to, one place to
name it by. A card opens that trip. The section appears by itself a moment after a new plan (the page
asks again while the answer is "pending"), and is simply not there when nothing is alike.

**Search** — `GET /trips/search?q=…` embeds the query and returns the traveller's trips that match it,
best first; the trips list has a search box above the cards. It finds by meaning: "somewhere quiet by the
sea" finds the beach trips. When nothing matches it says so; when the query cannot be embedded it says
that instead (`503`), and the list stays as it was. Emptying the box brings every trip back.

## Found and fixed along the way

| Problem | Fix |
|---|---|
| With the summary Phase 14 stored, a Goa beach trip was nearer a Ladakh trek than the Andaman beaches | A summary of what kind of trip it is; every stored vector made again (#140, #145) |
| An itinerary's two vectors could not be told apart: nothing said which was the summary | `embeddings.kind` (#145) |
| The first query for a traveller's trips, forced through the HNSW index, came back empty: the index's nearest vectors were all someone else's | An iterative scan (#142) |
| Startup recovery fired every pending embedding at once; the API's free tier answers that with 429s | One itinerary at a time (#145) |
| A plan's similar trips were asked for before its embedding existed | `"pending"`, and the page asks again (#147) |
| The test stand-in hashed words into dimensions: "zzzz" matched a trip by collision | Every word its own dimension (#149) |
| The query cache is in Redis, which the test stacks share with the dev servers: one run was handed another's vectors, under the real model's name | The stand-in has a model name of its own; the stale keys were deleted (#149) |
| The search's "best match is too far" check and its cut-off said the same thing twice — found because breaking one changed nothing | One rule |
| `HOW_TO_RUN.md` still told the reader to expect `501` from `/similar` | Corrected |

## Tests

- `tests/unit/test_phase23_similarity.py` — 24 tests: the summary text and what it leaves out; a stored
  text sent as a document and a typed one as a query, once; each row's `kind`; the highlight; the search
  cut-off; both routes (the matches as cards, two characters at least, 503, the query cache — a day, by
  the model's name, however the words are typed, with or without Redis — pending and ready, 404s); route
  order; recovery one at a time; the experiment's text is the application's.
- `tests/integration/test_phase23_similarity_integration.py` — 13 tests on real Postgres + pgvector, with
  trips placed at exact similarities: nearest first with the right scores; **only the traveller's own**;
  the latest itinerary, once per trip; summaries only, standing plans only; **five similar trips above the
  floor, with scores** (roadmap acceptance); nothing alike → none; not embedded → pending; the search
  window; **the HNSW index used and right** with seventy nearer trips of someone else's; the index holds
  summaries only; both endpoints over HTTP; **three trips planned through the whole pipeline — the beach
  trips find each other and not the trek** (roadmap acceptance); migration 004 up and down.
- `tests/unit/test_phase14_embeddings.py`, `tests/integration/test_phase14_integration.py` — the new
  summary, `kind` on the rows.
- `src/frontend/e2e/similar.spec.ts` — 8 tests: the beach trip's one similar trip, its card (destination,
  dates, total, highlight), under the map, opening the trip; a trip like no other has no section; the page
  asking again while pending; three cards of five; no section on an error; the search box — "beach",
  "monastery", a word nothing matches, back to every trip, opening a result; a search that cannot run;
  a phone.

Behaviours broken on purpose, one at a time — each caught by a test: other people's trips compared;
every version of a trip listed; full texts compared; failed trips listed; no similarity floor; a trip
similar to itself; no iterative scan; search returning everything above the floor; search with no floor;
a query embedded as a document; the kinds of places left out of the summary; rows without a `kind`; free
time counted as a place; the query cache ignoring the model, or the letter case; an embedding failure as
a 500; "ready" for "pending"; similar trips for a trip with no plan; recovery all at once; the migration
keeping the old vectors, or queueing every version; the page not asking again, showing five cards, or
keeping results in an emptied box.

**688 unit + 8 contract, 36 integration, 55 browser (5 end-to-end flows, 8 similar and search, 10 tips,
3 budget, 5 polish, 16 draft, 8 change-summary) — all passing.** The unit and contract suites also pass
in a clean `python:3.11-slim` container, `next build` succeeds, and the backend image rebuilds.

Verified on the real stack — backend on :8000, the dev frontend on :3000, the real embedding model,
vectors in the dev database (migrated; its six earlier trips were embedded again at startup by
themselves). As the demo account, with the twelve seeded trips:

```
similar to Goa:       South Goa 0.906 · Kovalam 0.857 · Havelock Island 0.857        (6–19 ms)
similar to Leh:       Manali 0.893
similar to Jaipur:    Udaipur 0.903 · Hampi 0.863 · Varanasi 0.851
search "forts and palaces":         Udaipur 0.704 · Jaipur 0.696                      (about 0.9 s: the query is embedded; 8 ms when asked again)
search "trekking in the mountains": Leh 0.663 · Manali 0.660
search "tea gardens":               Munnar 0.643
search "quarterly tax return":      nothing
```

Then a thirteenth trip was planned for real in the browser — Kochi, beaches and food, 19 s: `/similar`
answered pending, pending, ready, and the section appeared a second after the plan with Kovalam and Goa
in it. Both pages were checked at 1440 px and 375 px.

## Done criterion checklist

- [x] `GET /trips/{id}/similar`: up to 5 similar trips by pgvector cosine similarity, with scores
- [x] The query is the trip's summary embedding; summary and full text both tested, the result documented (#140)
- [x] Embedding experiments with at least 3 queries (13), strategy and reasons documented
- [x] HNSW vs IVFFlat documented (#142); the search answered from the HNSW index in a test
- [x] A Goa beach trip is similar to another beach trip, not a Ladakh trek — in the experiment, in a pipeline test, and live
- [x] "Similar Trips" on the trip page: 3 cards — destination, dates, total cost, a highlight — each opening its trip
- [x] `GET /trips/search?q=…`: the query embedded on the fly, matched against summary embeddings
- [x] A search input on the trips list, working end to end
- [x] DECISIONS.md updated (#140–#150)

## Known limits

- **Search does no arithmetic.** "beach under 50k 5 days" finds the beach trips, a ₹95,000 one among them:
  "under 50k" and "5 days" are words to an embedding. The roadmap gives those limits parameters of their
  own in a later phase (`budget_max`, `days_min`).
- **The cut-offs rest on twelve trips and thirteen queries**, hand-written, and belong to one model. They
  are constants with their reasoning beside them; the experiment is there to be run again.
- **Only the traveller's own trips** — so a first trip has no similar trips, and neither has a trip unlike
  the rest. Seeing other people's plans would be a feature of its own, with consent.
- **A trip is found a second or two after it is planned**, when its embedding is made; if the model is
  down then, not until the backend next starts (startup recovery).
- **The embedding API's free tier is tight**: a few dozen texts a minute. A search costs one (a repeated
  one none, for a day); a plan costs two.
- The full-text embedding is still written for every itinerary and nothing reads it (#140).
- The search box is submitted with Enter; searching while typing is the search phase's.
- CI still runs only the Python lint and unit suites (the CI phase is Phase 28).
