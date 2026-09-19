"""Embedding generation — Phase 14: real OpenAI text-embedding-3-small calls.

Called by orchestrator's persist_node immediately after an itinerary is saved.
Opens its own AsyncSession (via AsyncSessionLocal) so it does not block or
extend the caller's transaction — the HTTP response is already on its way
before this function touches the OpenAI API.

All exceptions are swallowed at the outer level so a failing embed call can
never crash the planning pipeline. The embedder writes a pending_retry row
as the fallback; startup recovery in main.py re-queues those rows.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


async def generate_embeddings(itinerary_id: uuid.UUID) -> None:
    """Generate and store two embedding rows for the given itinerary.

    Resolves the itinerary and its parent trip from the DB, then delegates
    to write_embedding_rows in src.ai.embeddings.embedder which handles
    the OpenAI call, retry logic, and pending_retry fallback.

    Safe to call from any context — orchestrator background task, FastAPI
    BackgroundTask, or startup recovery — because it always opens a fresh
    session.
    """
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
                logger.warning(
                    "[EMBEDDING] Itinerary %s not found — skipping", itinerary_id
                )
                return

            trip = await db.get(Trip, itinerary.trip_id)
            destination = trip.destination if trip else "Unknown"

            await write_embedding_rows(
                itinerary_id=itinerary_id,
                structured_data=itinerary.structured_data or {},
                destination=destination,
                total_cost=itinerary.total_cost,
                db=db,
            )

    except Exception as exc:
        logger.error(
            "[EMBEDDING] Unhandled error for itinerary %s: %s",
            itinerary_id,
            exc,
        )
