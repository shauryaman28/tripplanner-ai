"""Phase 30 — while a plan waits for anything outside the app, it holds no database connection.

Real Postgres, real Redis, the real graph, a whole plan through the HTTP API. What the load test
found (docs/load-test-report.md): a planning run kept a database connection — idle, inside a
transaction — from its first logged step to its last, and the embedding job after it kept another;
the number of plans the stack could make at once was the number of connections it had. Every place
that reads and then waits now ends its transaction in between.

That is a property of the whole run, and it is one statement away from being lost again — any new
`SELECT` before a model call brings it back. So it is asked of the whole run here: at the moment each
model, each tool and the embedding API is asked, the session of the code that is asking must not be
in a transaction.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase30_connections_integration.py -v
"""

import asyncio
import os
from contextvars import ContextVar
from unittest.mock import patch

import pytest

from app.api.routes import trips
from app.db import session as session_module
from src.ai.agents import destination_intelligence, preference_extractor
from src.ai.builder import builder
from src.ai.embeddings import embedder
from src.ai.orchestrator import orchestrator
from tests.fakes import fake_builder_llm, fake_embed, fake_intelligence_llm, flight
from tests.integration.test_pipeline_integration import _create_trip, _login, _stack, _until

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

# Every wait of a plan: three models, the preference extractor's, three tools, the embedding API.
WAITS = {"intent", "flights", "hotels", "attractions", "local tips", "the itinerary", "preferences", "embedding"}

# The session of the code under way. A planning run and the embedding job each make their own; a task
# starts with a copy of its starter's context, so whatever a run starts beside itself — its three
# searches, side by side — still sees the run's.
_session: ContextVar = ContextVar("the_session_of_the_code_under_way", default=None)


def sessions_are_remembered():
    """`AsyncSessionLocal`, as the app makes its sessions — and each one noted as its maker's."""
    real = session_module.AsyncSessionLocal

    def made(*args, **kwargs):
        session = real(*args, **kwargs)
        _session.set(session)
        return session

    return made


async def test_at_every_wait_of_a_plan_the_one_who_waits_holds_no_connection(db_session):
    held: dict[str, bool] = {}

    async def asked(what: str, after: float = 0.1) -> None:
        """Something outside the app has been asked, and `after` seconds of waiting for it have gone by:
        is the asker's session inside a transaction?

        Not looked at the instant it is asked: the two searches and the local tips run side by side
        on the run's one session, and one of them committing its row is, for that millisecond, a
        transaction. A wait is long; whatever is still open a tenth of a second into it was left
        open. The three that run together are looked at a tenth of a second apart for the same reason.
        """
        await asyncio.sleep(after)
        session = _session.get()
        assert session is not None, f"{what}: asked by code that never made a session"
        held[what] = session.in_transaction()

    async def intent(_message: str) -> dict:
        await asked("intent")
        return {}

    def a_search(what: str, found, after: float = 0.1):
        async def search(_tool: str, _params: dict):
            await asked(what, after)
            return found(_tool, _params) if callable(found) else found

        return search

    async def tips(system: str, prompt: str) -> str:
        await asked("local tips", after=0.3)
        return await fake_intelligence_llm(system, prompt)

    async def itinerary(system: str, prompt: str, on_token=None) -> str:
        await asked("the itinerary")
        return await fake_builder_llm(system, prompt, on_token)

    async def preferences(_system: str, _prompt: str) -> str:
        await asked("preferences")
        return '{"travel_style": "mid-range", "dietary_restrictions": [], "preferred_airlines": [], "home_city": null}'

    async def embedding(text: str, task_type: str = "") -> list[float]:
        await asked("embedding")
        return await fake_embed(text, task_type)

    remembered = sessions_are_remembered()
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, events, tools):
        from tests.fakes import ATTRACTIONS, HOTELS

        tools["flight"].side_effect = a_search("flights", lambda *_: [flight(8_200.0)])
        tools["hotel"].side_effect = a_search("hotels", HOTELS, after=0.1)
        tools["activities"].side_effect = a_search("attractions", ATTRACTIONS, after=0.2)
        with (
            patch.object(session_module, "AsyncSessionLocal", remembered),  # requests, the embedding job, cache warming
            patch.object(trips, "AsyncSessionLocal", remembered),  # the planning run
            patch.object(orchestrator, "_extract_intent", intent),
            patch.object(destination_intelligence, "_call_llm", tips),
            patch.object(builder, "_call_llm", itinerary),
            patch.object(preference_extractor, "_call_llm", preferences),
            patch.object(preference_extractor.settings, "GROQ_API_KEY", "a-key"),
            patch.object(embedder, "_call_embed", embedding),
        ):
            await _login(client)
            trip_id = await _create_trip(client)
            started = await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed long weekend in Goa"})
            assert started.status_code == 202

            # Heard on the trip's channel, not asked of the API: a request of this test's own would be
            # one more session at work, and the question here is what the plan's own code holds.
            async def planned():
                return any(event.get("event") == "planning_complete" for event in events)

            await _until(planned, "planning_complete")

            async def everything_was_asked():
                return WAITS <= set(held)

            await _until(everything_was_asked, f"every wait of the plan — still missing: {sorted(WAITS - set(held))}")
            await asyncio.sleep(0.2)  # the rows after the last wait: the extractor's, the embeddings'

            assert held == dict.fromkeys(WAITS, False), {what: held[what] for what in held if held[what]}

            # …and it is a plan like any other: saved, embedded, its preferences learned
            saved = await client.get(f"/trips/{trip_id}/itinerary")
            assert saved.status_code == 200 and saved.json()["structured_data"]["days"]
            runs = {run["agent_name"]: run["status"] for run in (await client.get(f"/trips/{trip_id}/runs")).json()}
            assert runs["orchestrator"] == "completed" and runs["preference_extractor"] == "completed"

            async def embedded():
                return (await client.get(f"/trips/{trip_id}/similar")).json()["status"] == "ready"

            await _until(embedded, "the itinerary's embeddings")

    # nothing is left lent once the plan and what followed it are done
    assert session_module.engine.pool.checkedout() == 0
