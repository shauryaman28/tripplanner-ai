"""AgentRun table — one row per agent execution.

This is the debugging superpower: when the re-planning logic breaks in
Phase 15, query agent_runs for the trip and read every decision.
No new logging code needed later — it's all already here.

status lifecycle: pending → running → completed | failed
input / output stored as JSONB so you can query them from psql.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    trip_id: uuid.UUID = Field(foreign_key="trips.id", index=True)

    agent_name: str = Field(max_length=100)      # e.g. "flight_agent", "orchestrator"
    status: str = Field(default="pending", max_length=20)

    input: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    output: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    duration_ms: Optional[int] = Field(default=None)   # wall-clock ms for the run
    created_at: datetime = Field(default_factory=datetime.utcnow)
