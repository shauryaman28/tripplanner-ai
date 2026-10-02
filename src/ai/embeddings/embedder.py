"""Phase 14 — Real embedding generation using Gemini `gemini-embedding-001`.

The roadmap names OpenAI text-embedding-3-small; Gemini is used instead so the
app needs no OpenAI key. It is asked for 1536 dimensions — the size of the
`embeddings.vector` column — so the schema is unchanged.

Two embeddings are written per itinerary:
  1. Full-text embedding   — all day/slot/hotel descriptions concatenated.
  2. Structured summary    — compact "{destination} N days M INR. Top activities: …"
     string that gives a stronger similarity signal for search (Phase 23).

Both rows land in the `embeddings` table with embedding_model=EMBEDDING_MODEL.

On any embedding failure the writer falls back to a single row with
embedding_model="pending_retry" and vector=NULL so the trip is still
marked completed and the system degrades gracefully. The startup recovery
in main.py re-queues these rows automatically.

The public seam for tests is `_call_embed`; patch it to avoid network calls.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlmodel import select
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential_jitter

try:
    from app.core.config import settings
except ImportError:
    from src.backend.app.core.config import settings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
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
    budget_label = "budget" if cost_int < 30_000 else "mid-range" if cost_int < 80_000 else "luxury"
    cost_str = f"{cost_int:,}" if cost_int else "unknown"
    activities_str = ", ".join(top_5) if top_5 else "sightseeing"
    return f"{destination} {num_days} days {cost_str} INR {budget_label}. Top activities: {activities_str}."


# ── Embedding call (the single network seam — patch this in tests) ────────


class EmbeddingNotConfigured(RuntimeError):
    """No API key — retrying cannot help."""


@retry(
    wait=wait_exponential_jitter(initial=1, max=30),
    stop=stop_after_attempt(4),
    retry=retry_if_not_exception_type(EmbeddingNotConfigured),
    reraise=True,
)
async def _call_embed(text: str) -> list[float]:
    """Embed one text with exponential back-off + jitter.

    tenacity retries on any exception (rate-limit 429s, transient network
    errors, 5xx) except a missing key. After 4 attempts the exception is
    re-raised so the caller can write a pending_retry row.
    """
    if not settings.GOOGLE_API_KEY:
        raise EmbeddingNotConfigured("GOOGLE_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent",
            headers={"x-goog-api-key": settings.GOOGLE_API_KEY},  # header, not URL: errors never carry the key
            json={"content": {"parts": [{"text": text}]}, "outputDimensionality": EMBEDDING_DIM},
        )
        response.raise_for_status()
        return response.json()["embedding"]["values"]


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
    If the embedding call fails after all retries:
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
        (
            await db.execute(
                select(Embedding).where(
                    Embedding.itinerary_id == itinerary_id,
                    Embedding.embedding_model == PENDING_RETRY_MODEL,
                )
            )
        )
        .scalars()
        .all()
    )
    for stale in stale_rows:
        await db.delete(stale)
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
            vector = await _call_embed(text)
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
