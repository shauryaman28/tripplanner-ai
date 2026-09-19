"""Phase 14 — Real embedding generation using OpenAI text-embedding-3-small.

Two embeddings are written per itinerary:
  1. Full-text embedding   — all day/slot/hotel descriptions concatenated.
  2. Structured summary    — compact "{destination} N days M INR. Top activities: …"
     string that gives a stronger similarity signal for search (Phase 23).

Both rows land in the `embeddings` table with embedding_model="text-embedding-3-small".

On any OpenAI failure the writer falls back to a single row with
embedding_model="pending_retry" and vector=NULL so the trip is still
marked completed and the system degrades gracefully. The startup recovery
in main.py re-queues these rows automatically.

The public seam for tests is `_call_openai_embed`; patch it to avoid network
calls, exactly as the tool tests patch the Amadeus client.
"""

from __future__ import annotations

import logging
import uuid

from sqlmodel import select
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
PENDING_RETRY_MODEL = "pending_retry"


# ── Text builders (pure functions — trivially unit-testable) ──────────────


def build_full_text(structured_data: dict) -> str:
    """Concatenate every named activity and hotel across all days.

    Produces a dense representation of the trip's full content — better for
    a "find me something similar to this entire itinerary" query.
    """
    parts: list[str] = []
    for day in structured_data.get("days", []):
        date_str = day.get("date", "")
        for slot_name in ("morning", "afternoon", "evening"):
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                parts.append(f"{date_str} {slot_name}: {slot['activity']}")
        hotel = day.get("hotel")
        if hotel and hotel.get("name"):
            parts.append(f"Hotel: {hotel['name']}")
    return ". ".join(parts) if parts else "No itinerary data available."


def build_summary_text(
    destination: str,
    num_days: int,
    total_cost: float | None,
    structured_data: dict,
) -> str:
    """Build the compact summary string that Phase 23 similarity search uses.

    Format: "{destination} {N} days {cost} INR {budget_range}. Top activities: a, b, c."

    Budget ranges:
      < ₹30,000  → budget
      ₹30–80k   → mid-range
      > ₹80,000  → luxury
    """
    activity_names: list[str] = []
    seen: set[str] = set()
    for day in structured_data.get("days", []):
        for slot_name in ("morning", "afternoon", "evening"):
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                name = slot["activity"]
                if name not in seen and name != "Explore the area":
                    activity_names.append(name)
                    seen.add(name)

    top_5 = activity_names[:5]
    cost_int = int(total_cost) if total_cost else 0
    budget_label = (
        "budget" if cost_int < 30_000
        else "mid-range" if cost_int < 80_000
        else "luxury"
    )
    cost_str = f"{cost_int:,}" if cost_int else "unknown"
    activities_str = ", ".join(top_5) if top_5 else "sightseeing"
    return (
        f"{destination} {num_days} days {cost_str} INR {budget_label}. "
        f"Top activities: {activities_str}."
    )


# ── OpenAI call (the single network seam — patch this in tests) ───────────


@retry(
    wait=wait_exponential_jitter(initial=1, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
async def _call_openai_embed(text: str) -> list[float]:
    """Call text-embedding-3-small with exponential back-off + jitter.

    tenacity retries on any exception (covers rate-limit 429s, transient
    network errors, and OpenAI 5xx). After 4 attempts the exception is
    re-raised so the caller can write a pending_retry row.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


# ── Row writer ────────────────────────────────────────────────────────────


async def write_embedding_rows(
    itinerary_id: uuid.UUID,
    structured_data: dict,
    destination: str,
    total_cost: float | None,
    db,  # AsyncSession — left un-typed to avoid circular imports
) -> None:
    """Write 2 embedding rows (full-text + summary) for one itinerary.

    Failure handling
    ─────────────────
    If the OpenAI call fails after all retries:
      1. Roll back any partially-added rows.
      2. Write ONE pending_retry row (vector=NULL, model="pending_retry").
      3. Commit the fallback row — the trip stays COMPLETED.

    Idempotency
    ────────────
    Any pre-existing pending_retry rows for this itinerary are deleted at the
    start of the function, so startup recovery (which re-calls generate_embeddings)
    never produces duplicate rows.
    """
    try:
        from app.models.embedding import Embedding
    except ImportError:
        from src.backend.app.models.embedding import Embedding

    # ── Clean up stale pending_retry rows (makes recovery idempotent) ──────
    stale_rows = (
        await db.execute(
            select(Embedding).where(
                Embedding.itinerary_id == itinerary_id,
                Embedding.embedding_model == PENDING_RETRY_MODEL,
            )
        )
    ).scalars().all()
    for stale in stale_rows:
        db.delete(stale)
    if stale_rows:
        await db.commit()
        logger.info(
            "[EMBEDDING] Cleared %d stale pending_retry row(s) for itinerary %s",
            len(stale_rows),
            itinerary_id,
        )

    # ── Build texts ─────────────────────────────────────────────────────────
    num_days = len(structured_data.get("days", []))
    texts = [
        build_full_text(structured_data),
        build_summary_text(destination, num_days, total_cost, structured_data),
    ]

    # ── Generate vectors and write rows ─────────────────────────────────────
    try:
        for text in texts:
            vector = await _call_openai_embed(text)
            db.add(
                Embedding(
                    itinerary_id=itinerary_id,
                    embedding_model=EMBEDDING_MODEL,
                    vector=vector,
                )
            )
        await db.commit()
        logger.info("[EMBEDDING] Wrote 2 rows for itinerary %s", itinerary_id)

    except Exception as exc:
        logger.error(
            "[EMBEDDING FAILED] itinerary=%s error=%s — writing pending_retry row",
            itinerary_id,
            exc,
        )
        try:
            await db.rollback()
            db.add(
                Embedding(
                    itinerary_id=itinerary_id,
                    embedding_model=PENDING_RETRY_MODEL,
                    vector=None,
                )
            )
            await db.commit()
        except Exception as inner:
            logger.error(
                "[EMBEDDING] Failed to write pending_retry row for %s: %s",
                itinerary_id,
                inner,
            )
