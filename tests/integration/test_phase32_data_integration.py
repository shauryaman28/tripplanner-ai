"""Phase 32 — the database goes away, and the backend dies: on a real Postgres.

docs/chaos-data-report.md is the whole of it on a running stack, with Postgres and the backend
killed for real. These are the parts that can be made to happen in a test, against the real
database: connections killed at the server under the pool that holds them; a save that loses its
connection half-way; a plan saved by a backend that then never embeds it.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase32_data_integration.py -v
"""

import asyncio
import os
from unittest.mock import patch

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.api.routes import trips
from app.db import session as session_module
from src.ai.orchestrator import orchestrator
from tests.fakes import flight
from tests.integration.test_pipeline_integration import _create_trip, _login, _run_finished, _stack, _until

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)


def lost(statement: str = "INSERT INTO itineraries (id, trip_id, structured_data) VALUES ($1, $2, $3)") -> DBAPIError:
    """What the app was handed when Postgres was killed under it: SQLAlchemy's error, the driver's beneath."""
    driver = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    wrapped = RuntimeError(f"<class 'asyncpg.exceptions.ConnectionDoesNotExistError'>: {driver}")
    wrapped.__cause__ = driver
    return DBAPIError(statement, {}, wrapped)


async def embeddings_of(db_session, itinerary_id: str) -> dict:
    counted = await db_session.execute(
        text(
            "select count(*) filter (where vector is not null), "
            "count(*) filter (where embedding_model = 'pending_retry') from embeddings where itinerary_id = :id"
        ),
        {"id": itinerary_id},
    )
    vectors, owed = counted.one()
    await db_session.commit()
    return {"vectors": vectors, "pending_retry": owed}


async def test_connections_killed_at_the_server_are_replaced_before_a_request_meets_one(db_session):
    """The roadmap's question — does `pool_pre_ping` find the dead connection and reconnect? — asked without a
    pooler in the way: every connection the app's pool holds is ended by the server, as a restart ends them."""
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        for _ in range(3):  # the pool now holds connections it believes are good
            assert (await client.get("/trips")).status_code == 200
        held = session_module.engine.pool.checkedin()
        assert held >= 1

        ended = await db_session.execute(
            text(
                "select count(pg_terminate_backend(pid)) from pg_stat_activity "
                "where datname = current_database() and pid <> pg_backend_pid()"
            )
        )
        assert ended.scalar() >= held
        await db_session.commit()

        answers = [await client.get("/trips") for _ in range(held + 2)]
        assert [answer.status_code for answer in answers] == [200] * len(
            answers
        )  # not one of them met a dead connection


async def test_a_request_that_finds_the_database_away_is_a_503_and_a_refused_statement_still_a_500(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        with patch.object(AsyncSession, "execute", side_effect=lost("SELECT users.id FROM users WHERE users.id = $1")):
            away = await client.get("/trips")
        assert away.status_code == 503 and away.headers["retry-after"] == "5"
        assert "try again shortly" in away.json()["detail"] and "asyncpg" not in away.text and "users" not in away.text

        assert (await client.get("/trips")).status_code == 200  # and it was nothing of the app's that broke

        a_bug = ProgrammingError("SELECT nope", {}, RuntimeError('column "nope" does not exist'))
        with patch.object(AsyncSession, "execute", side_effect=a_bug), pytest.raises(ProgrammingError):
            await client.get("/trips")  # raised on, to be the 500 it always was (the test client re-raises it)


async def test_a_save_that_loses_its_database_half_way_leaves_nothing_and_says_so_in_words(db_session):
    """Postgres killed between the two writes that save a plan. The rollback is Postgres's; what the app owes is
    not to show the traveller the statement, and not to leave the trip `planning`."""
    real_flush, failed = AsyncSession.flush, []

    async def flush(self, *args, **kwargs):
        if not failed and any(type(row).__name__ == "Itinerary" for row in self.new):
            failed.append(True)
            raise lost()  # the itinerary's insert is the statement the connection dies under
        return await real_flush(self, *args, **kwargs)

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        with patch.object(AsyncSession, "flush", flush), patch.object(trips, "SETTLE_EVERY_S", 0.05):
            assert (
                await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
            ).status_code == 202

            async def marked():
                return (await client.get(f"/trips/{trip_id}/status")).json()["status"] != "planning"

            await _until(marked, "the trip to be marked once its run had ended")

        status = (await client.get(f"/trips/{trip_id}/status")).json()
        assert status["status"] == "failed" and status["failure_reason"] == trips.DATABASE_AWAY
        (told,) = [event for event in events if event.get("event") == "planning_failed"]
        assert told["error"] == trips.DATABASE_AWAY  # not "(sqlalchemy.dialects.postgresql.asyncpg.Error) <class …"
        assert "INSERT" not in str(told) and "asyncpg" not in str(told)

        # neither of the two writes is there
        assert (await client.get(f"/trips/{trip_id}/itinerary")).status_code == 404
        rows = await db_session.execute(text("select count(*) from itineraries where trip_id = :id"), {"id": trip_id})
        assert rows.scalar() == 0
        await db_session.commit()

        # …and asked for again, the plan is saved whole
        assert (await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})).status_code == 202
        await _run_finished(client, trip_id, orchestrator_rows=1)
        assert (await client.get(f"/trips/{trip_id}/status")).json()["status"] == "completed"
        rows = await db_session.execute(text("select count(*) from itineraries where trip_id = :id"), {"id": trip_id})
        assert rows.scalar() == 1
        await db_session.commit()


async def test_a_plan_is_saved_owing_its_embeddings_and_the_next_start_pays(db_session):
    """The backend killed between saving an itinerary and embedding it. Before: no row at all, and nothing to
    find at the next start. Now the itinerary is saved *with* the row that says its embeddings are owed."""

    async def the_backend_dies_first(_itinerary_id) -> None:
        return None  # the job that would embed it never does anything

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        with patch.object(orchestrator, "generate_embeddings", the_backend_dies_first):
            await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
            await _run_finished(client, trip_id, orchestrator_rows=1)
        itinerary_id = (await client.get(f"/trips/{trip_id}/itinerary")).json()["id"]

        assert await embeddings_of(db_session, itinerary_id) == {"vectors": 0, "pending_retry": 1}
        assert (await client.get(f"/trips/{trip_id}/similar")).json()["status"] != "ready"

        await main._recover_after_restart()  # what a backend does as it starts

        async def embedded():
            return (await client.get(f"/trips/{trip_id}/similar")).json()["status"] == "ready"

        await _until(embedded, "the itinerary to be embedded by start-up recovery")
        assert await embeddings_of(db_session, itinerary_id) == {"vectors": 2, "pending_retry": 0}


async def test_a_plan_that_is_embedded_in_the_ordinary_way_owes_nothing_afterwards(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        await _login(client)
        trip_id = await _create_trip(client)
        await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "forts and food"})
        await _run_finished(client, trip_id, orchestrator_rows=1)
        itinerary_id = (await client.get(f"/trips/{trip_id}/itinerary")).json()["id"]

        async def embedded():
            return (await client.get(f"/trips/{trip_id}/similar")).json()["status"] == "ready"

        await _until(embedded, "the itinerary's embeddings")
        await asyncio.sleep(0.1)
        assert await embeddings_of(db_session, itinerary_id) == {
            "vectors": 2,
            "pending_retry": 0,
        }  # the owed row is gone
