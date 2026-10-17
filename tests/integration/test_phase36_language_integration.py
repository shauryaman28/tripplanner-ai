"""Phase 36 — a trip asked for in Hindi, through the public HTTP API, on real Postgres and Redis.

    RUN_INTEGRATION=1 pytest tests/integration/test_phase36_language_integration.py -v

The real routes, the real rows (migration 008's `preferred_language`), the real orchestrator,
builder, evaluator and PDF. The models are the stand-ins of tests/fakes.py: a translator that
puts the roadmap's request into English and writes "हिन्दी: …" for anything into Hindi, and a
builder that writes each stop's description in the language it is asked for.

The roadmap's acceptance, here:
  - a Hindi itinerary is stored in structured_data with Hindi text in its descriptions
  - preferred_language persists across sessions — signed out, signed in again, it is still there,
    and every trip after it is planned in it
"""

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from src.ai import language
from src.ai.agents.refinement_classifier import RefinementClassification
from src.ai.itinerary import SLOTS
from tests.fakes import flight
from tests.integration.test_pipeline_integration import END, START, _run_finished, _stack

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_INTEGRATION"),
    reason="Set RUN_INTEGRATION=1 to run integration tests (requires Docker)",
)

ACCEPTANCE = "गोवा में 5 दिन की यात्रा, बजट ₹50,000"
TRIP = {"start_date": str(START), "end_date": str(END), "budget": 50_000, "group_size": 1, "interests": ["history"]}


def stops(plan: dict) -> list[dict]:
    return [day[slot] for day in plan["days"] for slot in SLOTS if day.get(slot)]


async def _sign_in(client, creds: dict) -> None:
    signed_in = await client.post("/auth/login", data={"username": creds["email"], "password": creds["password"]})
    assert signed_in.status_code == 200, signed_in.text
    client.headers["Authorization"] = f"Bearer {signed_in.json()['access_token']}"


async def _new_account(client) -> dict:
    creds = {"email": f"{os.urandom(6).hex()}@example.com", "password": "testpass123"}
    assert (await client.post("/auth/register", json=creds)).status_code == 201
    await _sign_in(client, creds)
    return creds


async def _planned(client, destination: str, raw_input: str, runs_before: int = 0) -> tuple[str, dict, list[dict]]:
    """A trip made and planned. Returns (its id, the saved structured_data, the runs of the plan)."""
    created = await client.post("/trips", json={**TRIP, "destination": destination})
    assert created.status_code == 201, created.text
    trip_id = created.json()["id"]
    started = await client.post(f"/trips/{trip_id}/plan", json={"raw_input": raw_input})
    assert started.status_code == 202, started.text
    runs = await _run_finished(client, trip_id, orchestrator_rows=1)
    saved = await client.get(f"/trips/{trip_id}/itinerary")
    assert saved.status_code == 200, [(r["agent_name"], r["status"]) for r in runs]
    return trip_id, saved.json()["structured_data"], runs


def _agents(runs: list[dict]) -> list[str]:
    return [run["agent_name"] for run in runs]


async def test_a_hindi_request_is_planned_and_saved_in_hindi(db_session):
    """The roadmap's request, typed into a trip whose destination was typed in Hindi too."""
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, tools):
        await _new_account(client)
        trip_id, plan, runs = await _planned(client, "गोवा", ACCEPTANCE)

        # read, and put into English for everything after it
        assert _agents(runs)[:2] == ["language", "intent_parsing"]
        read = next(run for run in runs if run["agent_name"] == "language")
        assert read["status"] == "completed"
        assert read["output"]["language"] == "hi" and read["output"]["decided_by"] == "request"
        assert read["output"]["translated"] == {
            "raw_input": "A 5-day trip to Goa, budget ₹50,000",
            "destination": "Goa",
        }
        parsed = next(run for run in runs if run["agent_name"] == "intent_parsing")
        assert parsed["input"]["raw_input"] == "A 5-day trip to Goa, budget ₹50,000"
        assert {call.args[1]["destination"] for call in tools["activities"].await_args_list} == {"Goa"}
        # the same place, searched by its English name — and still the trip's name as it was typed (Phase 20)
        assert (await client.get(f"/trips/{trip_id}")).json()["destination"] == "गोवा"

        # stored in structured_data, in Hindi — and every name the plan goes by still in English
        assert plan["language"] == "hi"
        places = [stop for stop in stops(plan) if stop["activity"] != "Explore the area"]
        assert places and all(language.has_script(stop["description"], "hi") for stop in stops(plan))
        assert all(language.has_script(stop["local_name"], "hi") for stop in places)
        assert not any(language.has_script(stop["activity"], "hi") for stop in stops(plan))
        assert language.has_script(plan["local_intelligence"]["local_transport"], "hi")
        localised = next(run for run in runs if run["agent_name"] == "localise")
        assert localised["status"] == "completed" and localised["output"]["kept_in_english"] == 0
        assert _agents(runs).index("localise") == _agents(runs).index("persist") - 1

        # the timeline says what the two steps did
        timeline = (await client.get(f"/trips/{trip_id}/timeline")).json()
        steps = {step["agent_name"]: step for step in timeline}
        assert steps["language"]["detail"]["translated"] == ["raw_input", "destination"]
        assert steps["localise"]["detail"]["kept_in_english"] == 0

        # and the PDF prints it, in a font for Devanagari
        pdf = await client.get(f"/trips/{trip_id}/export/pdf")
        assert pdf.status_code == 200 and b"Devanagari" in pdf.content
        assert "trip-%E0%A4%97%E0%A5%8B%E0%A4%B5%E0%A4%BE-" in pdf.headers["content-disposition"]  # गोवा


async def test_a_chosen_language_persists_across_sessions_and_plans_every_trip_after_it(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
        creds = await _new_account(client)
        assert (await client.get("/users/preferences")).json()["preferred_language"] is None

        chosen = {"home_city": "Chennai", "preferred_language": "ta"}
        saved = await client.put("/users/preferences", json=chosen)
        assert saved.status_code == 200 and saved.json()["preferred_language"] == "ta"
        refused = await client.put("/users/preferences", json={"preferred_language": "fr"})
        assert refused.status_code == 422

        # signed out — the sign-in ended on the server — and in again: a new session, the same choice
        assert (await client.post("/auth/logout")).status_code == 204
        client.headers.pop("Authorization")
        client.cookies.clear()
        assert (await client.get("/users/preferences")).status_code == 401
        await _sign_in(client, creds)
        again = (await client.get("/users/preferences")).json()
        assert again["preferred_language"] == "ta" and again["home_city"] == "Chennai"

        # requests in English, plans in Tamil — this trip, and the next
        for destination in ("Goa", "Udaipur"):
            _, plan, runs = await _planned(client, destination, "Four relaxed days, history and food")
            read = next(run for run in runs if run["agent_name"] == "language")
            assert read["output"]["decided_by"] == "preference" and read["output"]["translated"] == {}
            assert plan["language"] == "ta" and all(language.has_script(s["description"], "ta") for s in stops(plan))

        # "as I write" again: the request decides, and an English one is planned in English
        assert (await client.put("/users/preferences", json={"home_city": "Chennai"})).json()[
            "preferred_language"
        ] is None
        _, plan, runs = await _planned(client, "Goa", "Four relaxed days, history and food")
        assert "language" not in plan and "language" not in _agents(runs) and "localise" not in _agents(runs)


async def test_a_change_to_a_hindi_plan_comes_back_in_hindi(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
        await _new_account(client)
        trip_id, _, _ = await _planned(client, "Goa", ACCEPTANCE)

        hotel = RefinementClassification(refinement_type="targeted_hotel", reason="a hotel near the beach")
        with patch("app.api.routes.trips.classify_refinement", AsyncMock(return_value=hotel)):
            changed = await client.post(f"/trips/{trip_id}/refine", json={"message": "होटल समुद्र तट के पास चाहिए"})
        assert changed.status_code == 200, changed.text
        runs = await _run_finished(client, trip_id, orchestrator_rows=2)
        turn_2 = [run for run in runs if run["turn"] == 2]
        assert _agents(turn_2) == [
            "refinement_classifier",
            "language",
            "hotel_agent",
            "itinerary_builder",
            "evaluator",
            "localise",
            "persist",
            "orchestrator",
        ]
        read = next(run for run in turn_2 if run["agent_name"] == "language")
        assert read["output"]["translated"] == {"refinement_request": "I want a hotel near the beach"}
        built = next(run for run in turn_2 if run["agent_name"] == "itinerary_builder")
        assert built["input"]["trip_meta"]["request"] == "I want a hotel near the beach"
        latest = (await client.get(f"/trips/{trip_id}/itinerary")).json()["structured_data"]
        assert latest["language"] == "hi" and all(language.has_script(s["description"], "hi") for s in stops(latest))


async def test_an_english_trip_is_planned_as_it_always_was(db_session):
    async with _stack(flight_tool=lambda *_: [flight(8_200.0)]) as (client, _events, _tools):
        await _new_account(client)
        _, plan, runs = await _planned(client, "Goa", "A relaxed long weekend in Goa")
    assert "language" not in _agents(runs) and "localise" not in _agents(runs)
    assert "language" not in plan
    assert all({"description", "local_name"}.isdisjoint(stop) for stop in stops(plan))


def test_migration_008_adds_the_language_and_comes_back_down():
    config = Config("alembic.ini")
    engine = create_engine(make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2"))

    def columns() -> dict[str, str]:
        with engine.begin() as db:
            rows = db.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'user_preferences'"
                )
            ).all()
        return dict(rows)

    assert columns()["preferred_language"] == "YES"  # nullable: nobody has chosen one yet
    user = uuid.uuid4()
    with engine.begin() as db:
        db.execute(
            text("INSERT INTO users (id, email, hashed_password, created_at) VALUES (:id, :email, 'x', now())"),
            {"id": user, "email": f"{user.hex[:12]}@example.com"},
        )
    try:
        with pytest.raises(IntegrityError), engine.begin() as db:  # the database holds to the four, too
            db.execute(
                text("INSERT INTO user_preferences (user_id, preferred_language) VALUES (:id, 'fr')"), {"id": user}
            )
        command.downgrade(config, "007")
        try:
            assert "preferred_language" not in columns()
        finally:
            command.upgrade(config, "head")
        assert columns()["preferred_language"] == "YES"
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        engine.dispose()
