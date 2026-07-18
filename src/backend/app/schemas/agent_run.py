import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class AgentRunRead(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    agent_name: str
    status: str
    input: Optional[Any]
    output: Optional[Any]
    duration_ms: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}
