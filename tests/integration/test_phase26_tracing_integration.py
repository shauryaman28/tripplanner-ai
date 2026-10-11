"""Phase 26 — tracing and token costs through the public HTTP API, on real Postgres and Redis.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase26_tracing_integration.py -v

The real routes, the real background run, the real `agent_runs` rows (migration
006's `token_count` and `cost_usd`) and their real sums. The models are
stand-ins that answer through LangChain, and LangSmith is a stand-in that
receives what the planner uploads (tests/fakes.py) — so what is checked is
that the two records of one run, the rows in the database and the trace, say
the same thing.

The last test runs the local trace receiver (scripts/trace_receiver.py) on a
loopback port and sends it a trace over real HTTP, as a backend pointed at it
with LANGCHAIN_ENDPOINT does.
"""

import importlib.util
import io
import logging
import os
import sys
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import langsmith
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlmodel import select

from app.core.config import settings
from app.models.agent_run import AgentRun
from app.models.trip import Trip
from app.models.user import User
from src.ai import tracing
from src.ai.agents.refinement_classifier import classify_refinement
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from src.ai.usage import PRICES, Usage
from src.ai.utils.run_logger import log_agent_run
from tests.fakes import flight, models_answer, network_stubs, traced
from tests.integration.test_pipeline_integration import _create_trip, _dear_flights, _login, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

LARGE, GEMINI = "openai/gpt-oss-120b", "gemini-3.5-flash-lite"
ASKED_A_MODEL = {"intent_parsing", "destination_intelligence", "itinerary_builder", "preference_extractor"}
A_NICER_HOTEL = '{"refinement_type": "targeted_hotel", "reason": "asks for another hotel"}'

# scripts/ is not a package: load the receiver the way `python scripts/trace_receiver.py` runs it.
_spec = importlib.util.spec_from_file_location(
    "trace_receiver", Path(__file__).resolve().parents[2] / "scripts" / "trace_receiver.py"
)
trace_receiver = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = trace_receiver
_spec.loader.exec_module(trace_receiver)


async def owner_of(db, trip_id: str) -> uuid.UUID:
    return (await db.execute(select(Trip.user_id).where(Trip.id == uuid.UUID(trip_id)))).scalar_one()


async def test_a_plan_writes_what_each_model_call_cost_and_its_trace_says_the_same(db_session):
    """One planning run, recorded twice: as rows in agent_runs, and as a trace. They must agree."""
    with traced() as langsmith:
        async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
            with models_answer(), patch("src.ai.llm.GEMINI_MODEL", GEMINI):
                await _login(client)
                trip_id = await _create_trip(client)
                await client.post(f"/trips/{trip_id}/plan", json={"raw_input": "A relaxed long weekend in Goa"})
                runs = await _run_finished(client, trip_id, orchestrator_rows=1)
            timeline = (await client.get(f"/trips/{trip_id}/timeline")).json()
            summary = (await client.get("/admin/cost-summary")).json()
            none_yet = (await client.get(f"/admin/cost-summary?since={date.today() + timedelta(days=1)}")).json()
        (trace,) = langsmith.traces(trip_id)

    # ── agent_runs: tokens and cost on the runs that asked a model, and on no other ──
    by_agent = {run["agent_name"]: run for run in runs}
    assert {name for name, run in by_agent.items() if run["token_count"] is not None} == ASKED_A_MODEL
    for name in ASKED_A_MODEL:
        assert by_agent[name]["token_count"] > 0 and by_agent[name]["cost_usd"] > 0, name
    for name in ("flight_agent", "hotel_agent", "activities_agent", "budget_decision", "evaluator", "persist"):
        assert (by_agent[name]["token_count"], by_agent[name]["cost_usd"]) == (None, None), name
    # the run's closing row adds nothing: every token is on the row of the agent that spent it, once
    assert by_agent["orchestrator"]["token_count"] is None
    tokens = sum(run["token_count"] or 0 for run in runs)
    dollars = sum(run["cost_usd"] or 0 for run in runs)

    # ── the trace: the same run, the same tokens, tagged with the trip and its owner ──
    assert trace.name == "plan trip" and trace.project == "tests"
    assert trace.tokens == tokens
    assert trace.metadata["trip_id"] == trip_id
    assert trace.metadata["user_id"] == str(await owner_of(db_session, trip_id))
    assert {span.metadata.get("user_id") for span in trace.walk()} == {trace.metadata["user_id"]}
    assert trace.find("extract_preferences")[0].tokens == by_agent["preference_extractor"]["token_count"]

    # ── the timeline shows them, step by step ──
    shown = {entry["agent_name"]: entry["token_count"] for entry in timeline if entry["event_type"] == "agent_run"}
    assert shown["itinerary_builder"] == by_agent["itinerary_builder"]["token_count"] and shown["evaluator"] is None

    # ── GET /admin/cost-summary: the same figures, added up ──
    assert summary["since"] == str(datetime.now(timezone.utc).date() - timedelta(days=30))
    assert summary["total"] == {"runs": 4, "tokens": tokens, "cost_usd": round(dollars, 6), "unpriced_tokens": 0}
    assert {line["agent"] for line in summary["by_agent"]} == ASKED_A_MODEL
    assert [line["cost_usd"] for line in summary["by_agent"]] == sorted(
        (line["cost_usd"] for line in summary["by_agent"]), reverse=True
    )  # dearest first
    assert sum(line["tokens"] for line in summary["by_agent"]) == tokens
    (today,) = summary["by_date"]
    assert today["date"] == str(datetime.now(timezone.utc).date()) and today["tokens"] == tokens
    assert none_yet["total"] == {"runs": 0, "tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0}
    assert none_yet["by_agent"] == [] and none_yet["by_date"] == []


async def test_a_refinement_is_classified_on_a_row_of_its_own_and_every_run_of_the_trip_is_a_trace(db_session):
    """Reading the request is a model call too. Until Phase 26 its tokens were counted nowhere."""
    with traced() as langsmith:
        async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
            with (
                models_answer(short=A_NICER_HOTEL),
                patch("src.ai.llm.GEMINI_MODEL", GEMINI),
                patch("app.api.routes.trips.classify_refinement", classify_refinement),  # the real one
            ):
                await _login(client)
                trip_id = await _create_trip(client)
                other_trip = await _create_trip(client, destination="Jaipur")
                await client.post(f"/trips/{trip_id}/plan")
                await _run_finished(client, trip_id, orchestrator_rows=1)
                await client.post(f"/trips/{other_trip}/plan")
                await _run_finished(client, other_trip, orchestrator_rows=1)

                answer = await client.post(f"/trips/{trip_id}/refine", json={"message": "A nicer hotel, please"})
                assert answer.status_code == 200 and answer.json()["refinement_type"] == "targeted_hotel"
                runs = await _run_finished(client, trip_id, orchestrator_rows=2)
        of_trip, of_other = langsmith.traces(trip_id), langsmith.traces(other_trip)

    # ── the classifier's own row, in the refinement's turn ──
    (classifier,) = [run for run in runs if run["agent_name"] == "refinement_classifier"]
    assert classifier["turn"] == 2 and classifier["status"] == "completed"
    assert classifier["input"] == {"message": "A nicer hotel, please"}
    assert classifier["output"]["refinement_type"] == "targeted_hotel"
    assert classifier["token_count"] > 0
    price_in, price_out = PRICES[GEMINI]
    assert 0 < classifier["cost_usd"] <= classifier["token_count"] * max(price_in, price_out) / 1e6

    # ── filtering by trip: this trip's three runs, and none of the other trip's ──
    assert [trace.name for trace in of_trip] == ["plan trip", "classify refinement", "refine trip: targeted_hotel"]
    assert {trace.project for trace in of_trip + of_other} == {
        "tests"
    }  # one project, whichever library began the trace
    assert [trace.name for trace in of_other] == ["plan trip"]
    owner = str(await owner_of(db_session, trip_id))
    for trace in of_trip:
        assert all(span.metadata["trip_id"] == trip_id and span.metadata["user_id"] == owner for span in trace.walk())
    classify, refine = of_trip[1], of_trip[2]
    assert classify.inputs == {"message": "A nicer hotel, please"}
    assert classify.outputs["refinement_type"] == "targeted_hotel"
    assert classify.tokens == classifier["token_count"]  # the row and the trace agree here too
    assert (refine.metadata["turn"], refine.metadata["detail"]) == (2, "targeted_hotel")
    assert refine.names() == ["hotel_agent", "build_itinerary", "evaluate", "persist", "merge"]


async def test_a_refinement_goes_ahead_when_its_classification_cannot_be_logged(db_session, caplog):
    """The row is bookkeeping. A traveller who asks for a nicer hotel is not refused because it could not be written."""

    async def no_row_for_the_classifier(*args, **kwargs):
        if kwargs.get("agent_name") == "refinement_classifier":
            raise RuntimeError("the database said no")
        return await log_agent_run(*args, **kwargs)

    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
        with (
            models_answer(short=A_NICER_HOTEL),
            patch("app.api.routes.trips.classify_refinement", classify_refinement),
            patch("app.api.routes.trips.log_agent_run", no_row_for_the_classifier),
            caplog.at_level(logging.WARNING, logger="app.api.routes.trips"),
        ):
            await _login(client)
            trip_id = await _create_trip(client)
            await client.post(f"/trips/{trip_id}/plan")
            await _run_finished(client, trip_id, orchestrator_rows=1)
            answer = await client.post(f"/trips/{trip_id}/refine", json={"message": "A nicer hotel, please"})
            runs = await _run_finished(client, trip_id, orchestrator_rows=2)

    assert answer.status_code == 200 and answer.json()["refinement_type"] == "targeted_hotel"
    assert "Could not log the classification of a refinement" in caplog.text
    second_turn = [run["agent_name"] for run in runs if run["turn"] == 2]
    assert "refinement_classifier" not in second_turn  # the row that could not be written…
    assert {"hotel_agent", "itinerary_builder", "persist"} <= set(second_turn)  # …and the change, made all the same


async def test_a_way_out_of_a_budget_conflict_is_a_trace_named_for_the_way_picked(db_session):
    """The plan that stopped at the budget and the re-plan that got through: two traces of one trip, told apart by name."""
    with traced() as langsmith:
        async with _stack(_dear_flights) as (client, _events, _tools):
            await _login(client)
            trip_id = await _create_trip(client, budget=40_000)
            await client.post(f"/trips/{trip_id}/plan")
            await _run_finished(client, trip_id, orchestrator_rows=1)
            await client.post(f"/trips/{trip_id}/replan", json={"choice": "cheaper_flights"})
            await _run_finished(client, trip_id, orchestrator_rows=2)
        stopped, went_ahead = langsmith.traces(trip_id)

    assert stopped.name == "plan trip" and stopped.names()[-1] == "escalate"
    assert went_ahead.name == "replan trip: cheaper_flights"
    assert (went_ahead.metadata["kind"], went_ahead.metadata["detail"]) == ("replan", "cheaper_flights")
    assert went_ahead.metadata["trip_id"] == trip_id and "replan" in went_ahead.tags
    assert "persist" in went_ahead.names()  # this one made a plan


async def a_trip(db) -> uuid.UUID:
    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password="x")
    db.add(user)
    await db.commit()
    trip = Trip(
        user_id=user.id, destination="Goa", start_date=date(2027, 1, 1), end_date=date(2027, 1, 3), budget=9_000
    )
    db.add(trip)
    await db.commit()
    return trip.id


def builds(count: int) -> list[Usage]:
    made = []
    for _ in range(count):
        usage = Usage()
        usage.add(LARGE, 2_400, 3_600)  # one itinerary
        made.append(usage)
    return made


async def test_ten_evaluator_retries_cross_the_limit_once_and_the_sum_is_the_databases(db_session, caplog):
    """The roadmap's alert test on real rows: the total is Postgres's SUM over the trip, not a number kept in memory."""
    dear, cheap, another = await a_trip(db_session), await a_trip(db_session), await a_trip(db_session)

    with caplog.at_level(logging.WARNING, logger="src.ai.utils.run_logger"):
        with patch.object(settings, "LLM_PRICES", {LARGE: (3.00, 15.00)}):  # a frontier model's price: 6 cents a build
            await log_agent_run(db_session, another, "itinerary_builder", {}, {}, 1, usage=builds(1)[0])
            for usage in builds(10):
                await log_agent_run(db_session, dear, "itinerary_builder", {}, {}, 4_800, status="failed", usage=usage)
                await log_agent_run(db_session, dear, "evaluator", {}, {"passed": False}, 0, status="failed")
        for usage in builds(10):  # the same ten at the price the model has: two and a half cents in all
            await log_agent_run(db_session, cheap, "itinerary_builder", {}, {}, 4_800, status="failed", usage=usage)

    alerts = [record.getMessage() for record in caplog.records if "[COST ALERT]" in record.getMessage()]
    assert len(alerts) == 1
    assert f"trip {dear}" in alerts[0] and "$0.1224 over 2 runs" in alerts[0] and "past the $0.10" in alerts[0]

    totals = dict(
        (
            await db_session.execute(
                text("SELECT trip_id, round(sum(cost_usd)::numeric, 4) FROM agent_runs GROUP BY trip_id")
            )
        ).all()
    )
    assert (float(totals[dear]), float(totals[cheap]), float(totals[another])) == (0.612, 0.0252, 0.0612)


async def test_the_cost_summary_says_which_tokens_it_could_not_price(db_session):
    trip_id = await a_trip(db_session)
    priced, unpriced = builds(1)[0], Usage()
    unpriced.add("a-model-nobody-priced", 700, 300)
    await log_agent_run(db_session, trip_id, "itinerary_builder", {}, {}, 1, usage=priced)
    await log_agent_run(db_session, trip_id, "intent_parsing", {}, {}, 1, usage=unpriced)
    await log_agent_run(db_session, trip_id, "evaluator", {}, {}, 1)  # asked no model: not in the summary at all
    old = AgentRun(trip_id=trip_id, agent_name="itinerary_builder", token_count=9_999, cost_usd=1.0)
    old.created_at = datetime(2026, 1, 15, 12, 0)
    db_session.add(old)
    await db_session.commit()

    async with _stack(flight_tool=lambda *_: []) as (client, _events, _tools):
        await _login(client)
        recent = (await client.get("/admin/cost-summary")).json()
        all_year = (await client.get("/admin/cost-summary?since=2026-01-01")).json()

    assert recent["total"] == {"runs": 2, "tokens": 7_000, "cost_usd": 0.00252, "unpriced_tokens": 1_000}
    assert recent["by_agent"] == [
        {"agent": "itinerary_builder", "runs": 1, "tokens": 6_000, "cost_usd": 0.00252, "unpriced_tokens": 0},
        {"agent": "intent_parsing", "runs": 1, "tokens": 1_000, "cost_usd": 0.0, "unpriced_tokens": 1_000},
    ]
    assert all_year["since"] == "2026-01-01"
    assert all_year["total"] == {"runs": 3, "tokens": 16_999, "cost_usd": 1.00252, "unpriced_tokens": 1_000}
    assert [day["date"] for day in all_year["by_date"]] == ["2026-01-15", str(datetime.now(timezone.utc).date())]


def test_migration_006_adds_the_two_columns_and_comes_back_down():
    config = Config("alembic.ini")
    engine = create_engine(make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2"))

    def columns() -> dict[str, tuple[str, str]]:
        with engine.begin() as db:
            rows = db.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'agent_runs'"
                )
            ).all()
        return {name: (kind, nullable) for name, kind, nullable in rows}

    assert columns()["token_count"] == ("integer", "YES")  # NULL: every run so far, and every run that asks no model
    assert columns()["cost_usd"] == ("double precision", "YES")
    command.downgrade(config, "005")
    try:
        assert not {"token_count", "cost_usd"} & set(columns())
    finally:
        # A downgrade that leaves a column behind must not outlive this test: 006 could never be applied
        # again, and this database — shared by every later run — would fail every test that touches it.
        with engine.begin() as db:
            db.execute(text("ALTER TABLE agent_runs DROP COLUMN IF EXISTS token_count, DROP COLUMN IF EXISTS cost_usd"))
        command.upgrade(config, "head")
    assert {"token_count", "cost_usd"} <= set(columns())
    engine.dispose()


async def test_the_local_receiver_takes_a_trace_over_http_and_finds_it_by_its_trip():
    """scripts/trace_receiver.py, as a backend with LANGCHAIN_ENDPOINT pointed at it uses it — sockets and all."""
    printed = io.StringIO()
    receiver = trace_receiver.Receiver(out=printed)
    server = trace_receiver.serve(0, receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    trip_id, other = uuid.uuid4(), uuid.uuid4()
    start = date.today() + timedelta(days=30)
    trip = {"destination": "Goa", "start_date": str(start), "end_date": str(start + timedelta(days=2))}

    client = tracing.make_client("any-key-will-do", url)
    tracing.use(client, "local")
    try:
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            for one in (trip_id, other):
                await OrchestratorAgent().run({**trip, "budget": 60_000.0, "interests": ["history"]}, trip_id=one)
        sender = threading.Thread(target=client.flush, daemon=True)
        sender.start()
        sender.join(15)
        assert not sender.is_alive(), "the tracing library could not send to the receiver"

        async with httpx.AsyncClient() as http:
            found = (await http.get(f"{url}/traces", params={"trip_id": str(trip_id)})).json()
            everything = (await http.get(f"{url}/traces")).json()
            records = (await http.get(f"{url}/records")).json()
        receiver.print_finished()

        # A client told not to batch sends each run on its own — POST /runs as it starts, PATCH /runs/<id>
        # as it ends — and the two must still end up as one run. Once with the library's own client…
        one_at_a_time = langsmith.Client(api_url=url, api_key="any-key-will-do", auto_batch_tracing=False)
        by_hand, raw = uuid.uuid4(), str(uuid.uuid4())
        one_at_a_time.create_run("sent on its own", {"asked": 1}, "chain", id=by_hand, project_name="local")
        one_at_a_time.update_run(by_hand, outputs={"answered": 2}, end_time=datetime.now(timezone.utc))
        async with httpx.AsyncClient() as http:
            # …and once as the API allows it: the run's id in the address only, not in what is sent
            await http.post(f"{url}/runs", json={"id": raw, "name": "sent raw", "run_type": "chain", "inputs": {}})
            await http.patch(f"{url}/runs/{raw}", json={"outputs": {"answered": 3}})
            sent_alone = {
                run["name"]: run for run in (await http.get(f"{url}/records")).json() if run["name"].startswith("sent ")
            }
    finally:
        tracing.use(None)
        server.shutdown()

    alone = sent_alone["sent on its own"]
    assert alone["id"] == str(by_hand) and alone["end_time"]
    assert (alone["inputs"], alone["outputs"]) == ({"asked": 1}, {"answered": 2})
    assert (sent_alone["sent raw"]["id"], sent_alone["sent raw"]["outputs"]) == (raw, {"answered": 3})

    (trace,) = found
    assert len(everything) == 2
    assert trace["name"] == "plan trip" and trace["metadata"]["trip_id"] == str(trip_id)
    assert {one["project"] for one in everything} == {"local"}  # the receiver says where each trace was sent
    assert [span["name"] for span in trace["spans"]][:3] == ["intent_parsing", "apply_preferences", "run_flight"]
    assert trace["tokens"] > 0 and trace["duration_ms"] >= 0
    # /records is everything, exactly as it was sent: the place to see what would leave the machine
    builds = [run for run in records if run["name"] == "build_itinerary"]
    assert len(builds) == 2 and all(run["inputs"]["destination"] == "Goa" for run in builds)
    assert all("db" not in run["inputs"] and "publish_fn" not in run["inputs"] for run in builds)
    # and it printed each trace as a tree
    drawn = printed.getvalue()
    assert drawn.count("plan trip   trip ") == 2 and f"trip {str(trip_id)[:8]}" in drawn
    assert "├─ build_itinerary" in drawn and "[llm]" in drawn and "tokens" in drawn
