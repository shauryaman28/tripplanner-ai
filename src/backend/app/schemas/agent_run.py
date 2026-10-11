import uuid
from typing import Any

from pydantic import BaseModel

from app.schemas.types import UTCDateTime


class AgentRunRead(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    agent_name: str
    status: str
    input: Any | None
    output: Any | None
    duration_ms: int | None
    # Phase 15: conversation turn (1-indexed).
    turn: int = 1
    # Phase 26: what the run asked of the models, and its list price in USD. None: no model was asked.
    token_count: int | None = None
    cost_usd: float | None = None
    created_at: UTCDateTime

    model_config = {"from_attributes": True}
