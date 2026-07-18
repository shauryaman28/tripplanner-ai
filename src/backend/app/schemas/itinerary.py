import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ItineraryRead(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    content: Optional[str]
    structured_data: Optional[Any]
    total_cost: Optional[float]
    created_at: datetime

    model_config = {"from_attributes": True}
