"""Embedding generation entry point — Phase 14.

Spawned as a background task by the orchestrator's persist_node right after an
itinerary is saved, and by startup recovery in main.py. Opens its own
AsyncSession so it never extends the caller's transaction.

All exceptions are swallowed at the outer level so a failing embed call can
never crash the planning pipeline. The embedder writes a pending_retry row
as the fallback; startup recovery re-queues those rows.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

# Redis counter of embedding jobs currently running — surfaced by GET /admin/embedding-health.
EMBEDDINGS_PENDING_KEY = "embeddings_pending"


async def _count_in_flight(delta: int) -> None:
    """Best-effort: the counter is a health signal, never a reason to fail a job."""
    try:
        try:
            from app.db import redis as redis_module
        except ImportError:
            from src.backend.app.db import redis as redis_module

        if redis_module.redis_client is not None:
            await redis_module.redis_client.incrby(EMBEDDINGS_PENDING_KEY, delta)
    except Exception:
        logger.debug("[EMBEDDING] could not update %s", EMBEDDINGS_PENDING_KEY, exc_info=True)


async def generate_embeddings(itinerary_id: uuid.UUID) -> None:
    """Generate and store two embedding rows for the given itinerary.

    Resolves the itinerary and its parent trip from the DB, then delegates
    to write_embedding_rows in src.ai.embeddings.embedder which handles
    the embedding call, retry logic, and pending_retry fallback.
    """
    await _count_in_flight(+1)
    try:
        try:
            from app.db.session import AsyncSessionLocal
            from app.models.itinerary import Itinerary
            from app.models.trip import Trip
        except ImportError:
            from src.backend.app.db.session import AsyncSessionLocal
            from src.backend.app.models.itinerary import Itinerary
            from src.backend.app.models.trip import Trip

        from src.ai.embeddings.embedder import write_embedding_rows

        async with AsyncSessionLocal() as db:
            itinerary = await db.get(Itinerary, itinerary_id)
            if itinerary is None:
                logger.warning("[EMBEDDING] Itinerary %s not found — skipping", itinerary_id)
                return

            trip = await db.get(Trip, itinerary.trip_id)
            await write_embedding_rows(
                itinerary_id=itinerary_id,
                structured_data=itinerary.structured_data or {},
                destination=trip.destination if trip else "Unknown",
                interests=trip.interests if trip else None,
                db=db,
            )

    except Exception as exc:
        logger.error("[EMBEDDING] Unhandled error for itinerary %s: %s", itinerary_id, exc)
    finally:
        await _count_in_flight(-1)
