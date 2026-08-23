"""Embedding generation — Phase 12 stub, real implementation lands in Phase 14.

Called by the orchestrator's persist_node immediately after an itinerary is
saved. Per the roadmap: "Trigger embedding stub generate_embeddings(itinerary_id)
— stub logs 'embedding pending' for now." Phase 14 replaces the body with real
OpenAI text-embedding-3-small calls and writes to the embeddings table
(2 rows per itinerary — full text + structured summary).
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


async def generate_embeddings(itinerary_id: uuid.UUID) -> None:
    """Stub — logs intent only. No I/O, never raises."""
    logger.info("[EMBEDDING PENDING] itinerary_id=%s — real generation lands in Phase 14", itinerary_id)
