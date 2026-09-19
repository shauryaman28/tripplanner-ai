import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
    created_at: datetime

    model_config = {"from_attributes": True}
