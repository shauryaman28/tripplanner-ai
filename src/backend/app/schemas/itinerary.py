import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ItineraryRead(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    content: str | None
    structured_data: Any | None
    total_cost: float | None
    created_at: datetime

    model_config = {"from_attributes": True}
