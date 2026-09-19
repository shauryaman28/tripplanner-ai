"""AgentRun table — one row per agent execution.

Phase 15 addition: `turn` column (integer, default 1) tracks which
conversation turn produced this row. GET /trips/{id}/runs?turn=N lets
callers isolate the second refinement pass from the initial plan.

status lifecycle: pending → running → completed | failed
input / output stored as JSONB so you can query them from psql.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    trip_id: uuid.UUID = Field(foreign_key="trips.id", index=True)

    agent_name: str = Field(max_length=100)  # e.g. "flight_agent", "orchestrator"
    status: str = Field(default="pending", max_length=20)

    input: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    output: dict | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    duration_ms: int | None = Field(default=None)

    # Phase 15: which conversation turn produced this row (1-indexed).
    # Default 1 means all pre-Phase-15 rows are treated as first-turn data.
    turn: int = Field(default=1)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
