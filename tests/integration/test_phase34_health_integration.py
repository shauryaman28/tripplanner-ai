"""Phase 34 — GET /admin/health-deep on the real things: Postgres and Redis from Docker, and the tool server
started as it is in production (`python -m src.ai.mcp_server.server`, a process of its own, over its pipes).
And the embedding queue the dashboard shows, counted from real rows.

Run with:
    RUN_INTEGRATION=1 pytest tests/integration/test_phase34_health_integration.py -v

LangSmith is never asked here: the test says it is not set up (a key in a developer's .env must not
send a request to it from a test run).
"""

import asyncio
import os
import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.models.embedding import Embedding
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from app.models.user import User
from src.ai.embeddings.embedder import PENDING_RETRY_MODEL

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

OPS = {"APP_ENV": "development", "OPS_TOKEN": "", "LANGCHAIN_API_KEY": ""}


async def deep_check() -> tuple[int, dict]:
    from app.main import app

    with patch.multiple(settings, **OPS):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answered = await client.get("/admin/health-deep")
    return answered.status_code, answered.json()


@pytest.mark.asyncio
async def test_on_real_postgres_redis_and_a_tool_server_process_every_dependency_answers(db_session):
    from app.db.redis import close_redis, init_redis
    from src.ai.mcp_client.client import close_session

    await close_session()  # whatever session an earlier test left: this one starts the real process
    await init_redis()
    try:
        status, body = await deep_check()
        again_status, again = await deep_check()  # the tool server is running now: asked, not started
    finally:
        await close_session()
        await close_redis()

    assert status == 200, body
    assert body["status"] == "ok"
    found = body["dependencies"]
    assert {name: result["status"] for name, result in found.items()} == {
        "postgres": "ok",
        "redis": "ok",
        "tool_server": "ok",
        "langsmith": "off",
    }
    assert found["tool_server"]["detail"].startswith("estimate_budget answered: ₹")
    # a real query and a real PING are milliseconds; the first tool call includes starting the process
    assert found["postgres"]["latency_ms"] < 1000 and found["redis"]["latency_ms"] < 1000
    assert (
        again_status == 200 and again["dependencies"]["tool_server"]["latency_ms"] < found["tool_server"]["latency_ms"]
    )


@pytest.mark.asyncio
async def test_with_redis_gone_the_deep_check_says_so_and_answers_503(db_session):
    from app.db import redis as redis_module
    from app.db.redis import close_redis, init_redis
    from src.ai.mcp_client.client import close_session

    await close_session()
    client = await init_redis()
    await client.connection_pool.disconnect()
    # a client that points at a port nothing listens on: what a Redis that went away looks like to the app
    import redis.asyncio as aioredis

    redis_module.redis_client = aioredis.from_url("redis://localhost:1/0", socket_connect_timeout=0.5)
    try:
        status, body = await deep_check()
    finally:
        await redis_module.redis_client.aclose()
        redis_module.redis_client = client
        await close_redis()
        await close_session()

    assert status == 503 and body["status"] == "failing"
    assert body["dependencies"]["redis"]["status"] == "failing"
    assert body["dependencies"]["postgres"]["status"] == "ok"  # …and the others were still asked
    assert body["dependencies"]["tool_server"]["status"] == "ok"


@pytest.mark.asyncio
async def test_the_embedding_queue_on_the_dashboard_is_the_rows_that_are_owed(db_session):
    from app import main
    from app.db.redis import close_redis, init_redis
    from src.telemetry import metrics

    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    owed = []
    for n in range(3):
        trip = Trip(
            user_id=user.id,
            destination="Goa",
            start_date=date.today() + timedelta(days=40),
            end_date=date.today() + timedelta(days=42),
            budget=40_000,
            status=TripStatus.COMPLETED,
        )
        db_session.add(trip)
        await db_session.commit()
        itinerary = Itinerary(trip_id=trip.id, structured_data={"days": []}, total_cost=30_000.0)
        db_session.add(itinerary)
        await db_session.commit()
        if n < 2:  # two whose embedding failed and is owed…
            db_session.add(Embedding(itinerary_id=itinerary.id, embedding_model=PENDING_RETRY_MODEL, vector=None))
            owed.append(itinerary.id)
        else:  # …and one that was embedded: its two rows are not owed
            for kind in ("full_text", "summary"):
                db_session.add(
                    Embedding(
                        itinerary_id=itinerary.id,
                        embedding_model="gemini-embedding-001",
                        kind=kind,
                        vector=[0.01] * 1536,
                    )
                )
    await db_session.commit()

    redis = await init_redis()
    try:
        await redis.set("embeddings_pending", 1)  # one being embedded right now
        counted = await main._embedding_queue()
        watching = asyncio.create_task(metrics.watch_embedding_queue(main._embedding_queue, every=0.05))
        await asyncio.sleep(0.2)
        watching.cancel()
        await redis.delete("embeddings_pending")
    finally:
        await close_redis()

    assert counted == {"in_flight": 1, "pending_retry": 2}
    assert metrics.EMBEDDING_QUEUE.labels("in_flight")._value.get() == 1.0
    assert metrics.EMBEDDING_QUEUE.labels("pending_retry")._value.get() == 2.0


@pytest.mark.asyncio
async def test_a_database_whose_host_name_does_not_resolve_is_known_as_away_by_what_the_driver_really_raises():
    """Found by the alert drill: a stopped pooler's name stopped resolving, and every request was a 500.

    The driver is asked for a real connection to a name that cannot resolve (`.invalid` never does), through
    SQLAlchemy as the app asks — whatever it raises, wrapped or not, is the database being away."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.errors import database_is_away

    engine = create_async_engine("postgresql+asyncpg://nobody:nothing@no-such-database.invalid:5432/none")
    try:
        with pytest.raises(Exception) as raised:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    assert database_is_away(raised.value), repr(raised.value)
