"""Embeddings of itineraries — Phase 14 (written), Phase 23 (searched).

Model: Gemini `gemini-embedding-001`, asked for 1536 dimensions — the size of
the `embeddings.vector` column. (The roadmap names OpenAI text-embedding-3-small;
Gemini is used so the app needs no OpenAI key.)

Two rows are written per itinerary, told apart by `kind`:

  summary     what kind of trip it is: the traveller's interests, the kinds of
              places, the places. This is what similarity and search compare.
  full_text   every slot and hotel of every day. Kept for comparison; nothing
              reads it (DECISIONS #140).

The summary's wording was chosen by experiment (scripts/embedding_experiment.py):
the summary Phase 14 stored — destination, length, cost and a budget word, then
the places — put a Goa beach trip nearer a Ladakh trek than the Andaman beaches.

The model is told what each text is for: a stored itinerary is a document
(RETRIEVAL_DOCUMENT), what someone types into the search box is a query
(RETRIEVAL_QUERY). Two itineraries are compared document to document.

On any embedding failure the writer falls back to a single row with
embedding_model="pending_retry" and vector=NULL so the trip is still
marked completed and the system degrades gracefully. The startup recovery
in main.py re-queues these rows automatically.

The seams for tests are `_call_embed` (a stored text) and `embed_query` (a typed
one): patch them to avoid network calls.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter

import httpx
from sqlmodel import select
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential_jitter

from src.ai.itinerary import FREE_TIME, SLOTS

try:
    from app.core.config import settings
except ImportError:
    from src.backend.app.core.config import settings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 1536
PENDING_RETRY_MODEL = "pending_retry"

# embeddings.kind — which of an itinerary's two texts a row is the vector of.
SUMMARY, FULL_TEXT = "summary", "full_text"

# What the model is told a text is for (the API's taskType).
DOCUMENT, QUERY = "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"

# A search waits for its query to be embedded: one attempt, and not for long.
QUERY_TIMEOUT_S = 8.0

SUMMARY_PLACES = 6  # places named in a summary
SUMMARY_KINDS = 3  # kinds of places named in it, the most common first


# ── Text builders (pure functions — trivially unit-testable) ──────────────


def build_full_text(structured_data: dict) -> str:
    """Concatenate every named activity and hotel across all days."""
    parts: list[str] = []
    for day in structured_data.get("days", []):
        date_str = day.get("date", "")
        for slot_name in SLOTS:
            slot = day.get(slot_name)
            if slot and slot.get("activity"):
                parts.append(f"{date_str} {slot_name}: {slot['activity']}")
        hotel = day.get("hotel")
        if hotel and hotel.get("name"):
            parts.append(f"Hotel: {hotel['name']}")
    return ". ".join(parts) if parts else "No itinerary data available."


def places_and_kinds(structured_data: dict) -> tuple[list[str], list[str]]:
    """(the places of an itinerary in the order they are visited, their categories from the most common down).

    Free time is not a place, and "sightseeing" — the category of a place the
    attractions search could not classify — says nothing about a trip.
    """
    names: list[str] = []
    kinds: Counter[str] = Counter()
    for day in structured_data.get("days") or []:
        for slot_name in SLOTS:
            slot = day.get(slot_name) or {}
            name = slot.get("activity")
            if name and name != FREE_TIME and name not in names:
                names.append(name)
                kinds[slot.get("category") or "sightseeing"] += 1
    return names, [kind for kind, _ in kinds.most_common() if kind != "sightseeing"]


def build_summary_text(destination: str, interests: list[str] | None, structured_data: dict) -> str:
    """What kind of trip an itinerary is, in a sentence or three — the text similarity and search compare.

        "beach and food trip to Goa. Kinds of places: beach, history, spiritual.
         Places: Baga Beach, Calangute Beach, Fort Aguada, Anjuna Beach."

    The kind of trip comes first, and nothing about its size is said: no
    length, no cost, no "mid-range". Those words made two trips of the same
    length and budget look alike whatever they were for (DECISIONS #140).
    """
    names, kinds = places_and_kinds(structured_data)
    wanted = [interest.strip() for interest in interests or [] if isinstance(interest, str) and interest.strip()]
    text = f"{' and '.join(wanted)} trip to {destination}." if wanted else f"Trip to {destination}."
    if kinds:
        text += f" Kinds of places: {', '.join(kinds[:SUMMARY_KINDS])}."
    if names:
        text += f" Places: {', '.join(names[:SUMMARY_PLACES])}."
    return text


# ── Embedding calls (the network seams — patch these in tests) ────────────


class EmbeddingNotConfigured(RuntimeError):
    """No API key — retrying cannot help."""


async def _embed_once(text: str, task_type: str, timeout: float) -> list[float]:
    if not settings.GOOGLE_API_KEY:
        raise EmbeddingNotConfigured("GOOGLE_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent",
            headers={"x-goog-api-key": settings.GOOGLE_API_KEY},  # header, not URL: errors never carry the key
            json={
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": EMBEDDING_DIM,
            },
        )
        response.raise_for_status()
        return response.json()["embedding"]["values"]


@retry(
    wait=wait_exponential_jitter(initial=1, max=30),
    stop=stop_after_attempt(4),
    retry=retry_if_not_exception_type(EmbeddingNotConfigured),
    reraise=True,
)
async def _call_embed(text: str, task_type: str = DOCUMENT) -> list[float]:
    """Embed one stored text with exponential back-off + jitter.

    tenacity retries on any exception (rate-limit 429s, transient network
    errors, 5xx) except a missing key. After 4 attempts the exception is
    re-raised so the caller can write a pending_retry row.
    """
    return await _embed_once(text, task_type, timeout=30)


async def embed_query(text: str) -> list[float]:
    """Embed what someone typed into the search box. One attempt: a request is waiting for it."""
    return await _embed_once(text, QUERY, timeout=QUERY_TIMEOUT_S)


# ── Row writer ────────────────────────────────────────────────────────────


async def write_embedding_rows(
    itinerary_id: uuid.UUID,
    structured_data: dict,
    destination: str,
    interests: list[str] | None,
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
    texts = {
        FULL_TEXT: build_full_text(structured_data),
        SUMMARY: build_summary_text(destination, interests, structured_data),
    }

    # ── Generate vectors and write rows ─────────────────────────────────────
    try:
        for kind, text in texts.items():
            vector = await _call_embed(text)
            db.add(
                Embedding(
                    itinerary_id=itinerary_id,
                    embedding_model=EMBEDDING_MODEL,
                    kind=kind,
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
