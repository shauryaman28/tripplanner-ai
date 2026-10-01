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
    created_at: UTCDateTime

    model_config = {"from_attributes": True}
