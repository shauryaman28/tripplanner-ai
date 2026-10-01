import uuid
from typing import Any

from pydantic import BaseModel

from app.schemas.types import UTCDateTime


class ItineraryRead(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    content: str | None
    structured_data: Any | None
    total_cost: float | None
    created_at: UTCDateTime

    model_config = {"from_attributes": True}
