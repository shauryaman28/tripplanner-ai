"""Similar trips and trip search — pgvector over itinerary summaries (Phase 23).

Both rank a traveller's own planned trips by the cosine similarity of one
vector to each trip's *summary* embedding (src/ai/embeddings/embedder.py):

    similar trips   the vector is the current trip's own summary
    search          the vector is what was typed into the search box, embedded on the spot

Three rules hold for both:

- Only the traveller's own trips. An itinerary says where someone is going and
  when: it is never shown to another account (DECISIONS #143).
- One result per trip — its latest itinerary. Every refinement saves a new
  itinerary with embeddings of its own; the earlier versions are history.
- A nearest neighbour is not necessarily near. Vector search always has an
  answer, so each use has a cut-off below which a trip is not a match at all.
  The numbers were measured on gemini-embedding-001 (scripts/embedding_experiment.py)
  and belong to it: another model, or another summary text, needs them measured again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from src.ai.embeddings.embedder import SUMMARY
from src.ai.itinerary import FREE_TIME, SLOTS

# ── Similar trips ──────────────────────────────────────────────────────────

SIMILAR_LIMIT = 5
# Summary to summary. In the experiment two trips of the same kind scored 0.850–0.906 and two of
# different kinds 0.782–0.859: at 0.84 every same-kind pair is kept and 4 of the 55 others — each a
# near miss worth showing (Hampi's temples and Varanasi's; the Kerala coast and the Kerala hills).
SIMILAR_FLOOR = 0.84

# ── Search ─────────────────────────────────────────────────────────────────

SEARCH_LIMIT = 10
# A typed query to a summary. The best match of a real query scored 0.614–0.730, of a nonsense one
# ("asdfgh qwerty", "quarterly tax return") 0.478–0.554: below 0.58 nothing matches.
SEARCH_FLOOR = 0.58
# …and of the trips above it, those nearly as close as the best one are the answer. Within 0.04 of
# the best, 30 of the 34 trips returned for thirteen queries were right and 2 right ones were missed.
SEARCH_WINDOW = 0.04


@dataclass(frozen=True)
class Match:
    """A trip, the itinerary it was matched on, and how close it came (cosine similarity, 1 = the same)."""

    trip: Trip
    itinerary: Itinerary
    similarity: float

    @property
    def highlight(self) -> str | None:
        return highlight(self.itinerary.structured_data)


def highlight(structured_data: dict | None) -> str | None:
    """The one place to name a trip by: its most popular stop — the first of them, if several are."""
    best: tuple[float, str] | None = None
    for day in (structured_data or {}).get("days") or []:
        for slot_name in SLOTS:
            slot = day.get(slot_name) or {}
            name, rating = slot.get("activity"), slot.get("rating")
            if not name or name == FREE_TIME:
                continue
            score = float(rating) if isinstance(rating, (int, float)) and not isinstance(rating, bool) else 0.0
            if best is None or score > best[0]:
                best = (score, name)
    return best[1] if best else None


def _latest_itinerary_id(trip_id):
    """Scalar subquery: the id of a trip's latest itinerary."""
    return (
        select(Itinerary.id)
        .where(Itinerary.trip_id == trip_id)
        .order_by(Itinerary.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )


async def summary_vector(db: AsyncSession, trip_id: uuid.UUID) -> list[float] | None:
    """The summary embedding of a trip's latest itinerary; None while it has not been made (or could not be)."""
    vector = await db.scalar(
        select(Embedding.vector).where(
            Embedding.itinerary_id == _latest_itinerary_id(trip_id),
            Embedding.kind == SUMMARY,
            Embedding.vector.is_not(None),
        )
    )
    return None if vector is None else [float(value) for value in vector]


async def nearest_trips(
    db: AsyncSession,
    vector: list[float],
    user_id: uuid.UUID,
    *,
    limit: int,
    floor: float = 0.0,
    exclude_trip: uuid.UUID | None = None,
) -> list[Match]:
    """A user's planned trips nearest to `vector`, nearest first — at most `limit`, none below `floor`."""
    distance = Embedding.vector.cosine_distance(vector)
    query = (
        select(Trip, Itinerary, (1 - distance).label("similarity"))
        .join(Itinerary, Itinerary.trip_id == Trip.id)
        .join(Embedding, Embedding.itinerary_id == Itinerary.id)
        .where(
            Embedding.kind == SUMMARY,  # the partial HNSW index's own condition
            Trip.user_id == user_id,
            # a trip whose plan stands — a change in flight ("planning") leaves the plan it is changing in place
            Trip.status.in_((TripStatus.COMPLETED, TripStatus.PLANNING)),
            Itinerary.id == _latest_itinerary_id(Trip.id).correlate(Trip),
            distance <= 1 - floor,
        )
        .order_by(distance)
        .limit(limit)
    )
    if exclude_trip is not None:
        query = query.where(Trip.id != exclude_trip)

    # When the planner answers from the HNSW index, the index hands back its nearest vectors and the
    # conditions above are applied to those: with other people's trips nearer, a traveller's own could
    # all be filtered away. An iterative scan (pgvector 0.8+) keeps going until the limit is met.
    try:
        async with db.begin_nested():
            await db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    except Exception:
        pass  # an older pgvector: the query below is still right whenever the planner scans the table

    rows = (await db.execute(query)).all()
    return [Match(trip=trip, itinerary=itinerary, similarity=float(similarity)) for trip, itinerary, similarity in rows]


async def similar_trips(db: AsyncSession, trip: Trip, limit: int = SIMILAR_LIMIT) -> list[Match] | None:
    """The traveller's other trips most like this one. None while this trip's own embedding is not there yet."""
    vector = await summary_vector(db, trip.id)
    if vector is None:
        return None
    return await nearest_trips(db, vector, trip.user_id, limit=limit, floor=SIMILAR_FLOOR, exclude_trip=trip.id)


def best_matches(matches: list[Match]) -> list[Match]:
    """Of the nearest trips to a query, the ones that answer it: those close to the best — and none below the floor.

    So when even the best match is under the floor, nothing is returned at all.
    """
    if not matches:
        return []
    cut_off = max(SEARCH_FLOOR, matches[0].similarity - SEARCH_WINDOW)
    return [match for match in matches if match.similarity >= cut_off]


async def search_trips(db: AsyncSession, user_id: uuid.UUID, query_vector: list[float]) -> list[Match]:
    """The traveller's trips that match a query, given the query's embedding."""
    return best_matches(await nearest_trips(db, query_vector, user_id, limit=SEARCH_LIMIT))
