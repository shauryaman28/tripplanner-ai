"""Import all table models here so SQLModel.metadata is fully populated
before Alembic autogeneration or metadata.create_all() runs.
"""

from app.models.agent_run import AgentRun
from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User

__all__ = ["User", "Trip", "TripStatus", "Itinerary", "AgentRun", "Embedding"]
