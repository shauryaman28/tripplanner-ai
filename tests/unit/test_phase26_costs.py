"""Phase 26 — what the models are asked, what it costs, and where that is written down.

The roadmap's acceptance for token cost tracking:

- `agent_runs.token_count` is populated after each run — the rows are checked
  here as the agents write them, and against a real database in
  tests/integration/test_phase26_tracing_integration.py;
- the over-cost alert fires in a test simulating ten evaluator retries
  (`test_ten_evaluator_retries_…`) — and does not, at the prices the models
  actually have, which is the more useful half of that test.

The models are stand-ins that answer through LangChain (tests/fakes.py), so the
callback that reads token usage is the one that runs on the real stack.
"""

import asyncio
import logging
import uuid
from datetime import date, timedelta
from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.graph import END, StateGraph

from app.core.config import settings
from app.core.security import create_access_token
from app.models.agent_run import AgentRun
from app.schemas.agent_run import AgentRunRead
from src.ai import usage as usage_module
from src.ai.agents import flight_agent
from src.ai.agents.destination_intelligence import DestinationIntelligenceAgent
from src.ai.agents.flight_agent import FlightAgent
from src.ai.builder.builder import ItineraryBuilder
from src.ai.orchestrator.orchestrator import intent_parsing_node
from src.ai.usage import PRICES, Usage, metered, price_of
from src.ai.utils import run_logger
from src.ai.utils.run_logger import log_agent_run, timed_run
from tests.fakes import ATTRACTIONS, HOTELS, FakeChatModel, flight, models_answer

START = date.today() + timedelta(days=30)
TRIP = {"destination": "Goa", "start_date": str(START), "end_date": str(START + timedelta(days=2)), "group_size": 2}
LARGE, SMALL, GEMINI = "openai/gpt-oss-120b", "openai/gpt-oss-20b", "gemini-3.5-flash-lite"
BUILD = (2_400, 3_600)  # what one itinerary asks of the builder's model: tokens in, tokens out


def cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """List price of a call, worked out by hand from the table."""
    price_in, price_out = PRICES[model]
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 8)


def asked(model: str, tokens_in: int, tokens_out: int) -> Usage:
    usage = Usage()
    usage.add(model, tokens_in, tokens_out)
    return usage


class Rows:
    """A database session, as far as writing an agent_runs row needs one: it keeps what is added.

    `scalar` answers the two questions the cost alert asks, in the order it
    asks them: what the trip's runs cost in all, and how many of them there are.
    """

    def __init__(self) -> None:
        self.rows: list[AgentRun] = []
        self.asked = 0

    def add(self, row: AgentRun) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        pass

    async def refresh(self, _row: AgentRun) -> None:
        pass

    async def scalar(self, _statement) -> float | int:
        self.asked += 1
        priced = [row.cost_usd for row in self.rows if row.cost_usd is not None]
        return sum(priced) if self.asked % 2 else len(priced)

    def named(self, agent: str) -> AgentRun:
        return next(row for row in self.rows if row.agent_name == agent)


# ── Prices ─────────────────────────────────────────────────────────────────


def test_tokens_are_priced_at_their_models_list_price():
    builder_call = asked(LARGE, 2_400, 3_600)  # the reasoning a model does is in its output tokens, and billed so

    assert builder_call.token_count == 6_000
    assert builder_call.cost_usd == cost(LARGE, 2_400, 3_600) == 0.00306  # $0.15 and $0.75 a million
    assert asked(SMALL, 1_000_000, 1_000_000).cost_usd == 0.60
    assert asked(GEMINI, 300, 40).cost_usd == round((300 * 0.30 + 40 * 2.50) / 1e6, 8)

    two_models = asked(LARGE, 1_000, 1_000)
    two_models.add(GEMINI, 1_000, 1_000)
    two_models.add(LARGE, 1_000, 0)  # the same model again: added to, not replaced
    assert two_models.by_model == {LARGE: [2_000, 1_000], GEMINI: [1_000, 1_000]}
    assert two_models.cost_usd == round(cost(LARGE, 2_000, 1_000) + cost(GEMINI, 1_000, 1_000), 8)


def test_a_dated_or_prefixed_name_is_priced_as_the_model_it_is():
    assert price_of(LARGE) == (0.15, 0.75)
    assert price_of("models/gemini-3.5-flash-lite") == (0.30, 2.50)  # as Google's API names it
    assert price_of("gemini-3.5-flash-lite-preview-09-2026") == (0.30, 2.50)  # a version of it
    assert price_of("gemini-3.5-flash") is None  # not the same model as its "-lite": no price on record
    assert price_of("claude-something") is None


def test_a_price_is_added_or_corrected_in_the_settings_without_touching_the_code():
    with patch.object(settings, "LLM_PRICES", {"gemini-3.5-flash": (0.50, 3.00), LARGE: (1.0, 2.0)}):
        assert price_of("gemini-3.5-flash") == (0.50, 3.00)
        assert price_of("gemini-3.5-flash-lite") == (0.30, 2.50)  # the longer name still wins for the model it is
        # a version of either is priced as the longest name it starts with: "-flash-lite-001" is the lite
        assert price_of("gemini-3.5-flash-lite-001") == (0.30, 2.50)
        assert price_of("models/gemini-3.5-flash-002") == (0.50, 3.00)
        assert asked(LARGE, 1_000_000, 1_000_000).cost_usd == 3.0  # corrected

    assert price_of("gemini-3.5-flash") is None and price_of(LARGE) == (0.15, 0.75)


def test_a_model_without_a_price_has_its_tokens_counted_and_its_cost_left_out(caplog):
    """Never guessed. A total with such tokens in it is a lower bound, and the log says which model to price."""
    usage_module._unpriced.discard("a-new-model")
    unknown = asked("a-new-model", 900, 100)

    with caplog.at_level(logging.WARNING, logger="src.ai.usage"):
        assert unknown.token_count == 1_000 and unknown.cost_usd is None
        assert asked("a-new-model", 5, 5).cost_usd is None

        partly = asked("a-new-model", 900, 100)
        partly.add(LARGE, 1_000, 1_000)
        assert partly.token_count == 3_000 and partly.cost_usd == cost(LARGE, 1_000, 1_000)

    warnings = [record.getMessage() for record in caplog.records if "no price on record" in record.getMessage()]
    assert len(warnings) == 1 and "'a-new-model'" in warnings[0] and "LLM_PRICES" in warnings[0]  # said once


def test_work_that_asked_no_model_has_no_tokens_which_is_not_zero_tokens():
    with metered() as idle:
        pass

    assert idle.token_count is None and idle.cost_usd is None and idle.by_model == {}


# ── The meter ──────────────────────────────────────────────────────────────


def model(name: str, tokens_in: int, tokens_out: int, **options) -> FakeChatModel:
    return FakeChatModel(model=name, answer="ok", tokens=(tokens_in, tokens_out), **options)


class _State(TypedDict, total=False):
    inside: dict


async def _an_agent(_state: _State) -> _State:
    """An agent inside a graph: it times its own work, as every agent does."""
    with metered() as mine:
        await model(LARGE, 1_000, 2_000).ainvoke("build")
    return {"inside": {"tokens": mine.token_count, "cost": mine.cost_usd}}


async def test_tokens_are_credited_to_the_innermost_meter_and_to_no_other():
    """The orchestrator times a run; inside it every agent times its part. Counted in both, every token is counted twice.

    LangChain's own `get_usage_metadata_callback` does count both here — the
    graph is started under the outer meter and hands its callbacks down.
    """
    graph = StateGraph(_State)
    graph.add_node("agent", _an_agent)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)

    with metered() as whole_run:
        result = await graph.compile().ainvoke({})
        assert whole_run.token_count is None  # the agent's 3,000 are the agent's
        await model(GEMINI, 80, 10).ainvoke("classify")  # asked with no meter of its own: the run's

    assert result["inside"] == {"tokens": 3_000, "cost": cost(LARGE, 1_000, 2_000)}
    assert whole_run.by_model == {GEMINI: [80, 10]}


async def test_work_done_side_by_side_is_counted_apart():
    async def one(name: str, tokens: int) -> Usage:
        with metered() as mine:
            await model(name, tokens, tokens).ainvoke("x")
            await asyncio.sleep(0.01)  # the other one runs in between
            await model(name, tokens, tokens).ainvoke("x")
        return mine

    with metered() as around:
        hotels, tips = await asyncio.gather(one(LARGE, 10), one(SMALL, 500))

    assert hotels.by_model == {LARGE: [20, 20]} and tips.by_model == {SMALL: [1_000, 1_000]}
    assert around.token_count is None


async def test_a_meter_is_credited_nothing_once_its_block_has_ended():
    """One task, one thing after another — as a route that classifies a request and then goes on."""
    with metered() as whole_run:
        with metered() as first:
            await model(LARGE, 100, 50).ainvoke("x")
        await model(GEMINI, 80, 10).ainvoke("asked between two agents: the run's")
        with metered() as second:
            await model(SMALL, 7, 3).ainvoke("x")

    await model(LARGE, 9, 9).ainvoke("asked with no meter open: counted nowhere, and no error")

    assert first.by_model == {LARGE: [100, 50]}
    assert second.by_model == {SMALL: [7, 3]}
    assert whole_run.by_model == {GEMINI: [80, 10]}


async def test_a_streamed_reply_is_counted_once_whatever_the_number_of_pieces():
    with metered() as streamed:
        pieces = [piece async for piece in FakeChatModel(model=LARGE, answer="x" * 240, tokens=(700, 60)).astream("go")]

    assert len(pieces) > 5  # it did come in pieces
    assert streamed.by_model == {LARGE: [700, 60]}


async def test_a_provider_that_reports_no_usage_and_a_call_that_fails_claim_nothing():
    with metered() as nothing_said:
        await model(LARGE, 0, 0, silent=True).ainvoke("x")
    assert nothing_said.token_count is None  # not 0: nobody knows

    with metered() as failed:
        with pytest.raises(RuntimeError):
            await FakeChatModel(model=LARGE, answer=RuntimeError("429")).ainvoke("x")
    assert failed.token_count is None


async def test_timed_run_measures_the_time_and_what_the_models_were_asked():
    async with timed_run() as timer:
        await model(LARGE, 100, 50).ainvoke("x")
        await asyncio.sleep(0.02)

    assert timer.duration_ms >= 20
    assert timer.usage.token_count == 150 and timer.usage.cost_usd == cost(LARGE, 100, 50)


# ── The rows ───────────────────────────────────────────────────────────────


async def test_a_logged_run_carries_its_tokens_and_their_cost():
    db, trip_id = Rows(), uuid.uuid4()

    with_model = await log_agent_run(db, trip_id, "itinerary_builder", {}, {}, 4_800, usage=asked(LARGE, 2_400, 3_600))
    without = await log_agent_run(db, trip_id, "flight_agent", {}, {}, 20)
    asked_nobody = await log_agent_run(db, trip_id, "evaluator", {}, {}, 0, usage=Usage())

    assert (with_model.token_count, with_model.cost_usd) == (6_000, 0.00306)
    assert (without.token_count, without.cost_usd) == (None, None)
    assert (asked_nobody.token_count, asked_nobody.cost_usd) == (None, None)
    # …and they are what the API hands out
    read = AgentRunRead.model_validate(with_model)
    assert (read.token_count, read.cost_usd) == (6_000, 0.00306)


@pytest.mark.parametrize("streamed", [False, True], ids=["in one piece", "streamed"])
async def test_the_builders_row_has_what_the_builder_spent(streamed):
    db, pieces = Rows(), []

    async def on_token(piece: str) -> None:
        pieces.append(piece)

    with models_answer(tokens=BUILD) as models:  # the stand-in reports what a real build of this size does
        built = await ItineraryBuilder().run(
            TRIP,
            [flight(8_200.0)],
            HOTELS,
            ATTRACTIONS,
            db=db,
            trip_id=uuid.uuid4(),
            on_token=on_token if streamed else None,
        )

    assert built["error"] is None and bool(pieces) is streamed
    (asked_model,) = models
    assert asked_model.model == LARGE
    row = db.named("itinerary_builder")
    assert (row.token_count, row.cost_usd) == (6_000, cost(LARGE, *BUILD))  # to the token, streamed or not


async def test_each_agents_row_has_its_own_tokens_and_a_search_has_none():
    db, trip_id = Rows(), uuid.uuid4()
    state = {**TRIP, "raw_input": "A relaxed long weekend", "db": db, "trip_id": trip_id, "interests": ["history"]}

    with (
        models_answer(short='{"interests": ["beach"]}') as models,
        patch("src.ai.llm.GEMINI_MODEL", GEMINI),
        patch.object(flight_agent, "call_tool", AsyncMock(return_value=[flight(8_200.0)])),
    ):
        await intent_parsing_node(state)  # Gemini
        await DestinationIntelligenceAgent().run(state, db=db, trip_id=trip_id)  # Groq
        await FlightAgent().run(
            {"origin": "DEL", "destination": "Goa", "date": str(START), "budget": 60_000}, db=db, trip_id=trip_id
        )

    assert [m.model for m in models] == [GEMINI, LARGE]
    parsing, tips, search = db.named("intent_parsing"), db.named("destination_intelligence"), db.named("flight_agent")
    assert parsing.token_count > 0 and parsing.cost_usd > 0
    assert tips.token_count > 0 and tips.cost_usd > 0
    assert parsing.token_count != tips.token_count  # each its own, not a shared total
    assert (search.token_count, search.cost_usd) == (None, None)  # a search is plain code: it asked no model


# ── The alert ──────────────────────────────────────────────────────────────


async def retries(db: Rows, trip_id: uuid.UUID, how_many: int) -> None:
    """An evaluator that keeps sending the plan back: the builder writes it again, and again."""
    for _ in range(how_many):
        await log_agent_run(
            db, trip_id, "itinerary_builder", {}, {}, 4_800, status="failed", usage=asked(LARGE, *BUILD)
        )
        await log_agent_run(db, trip_id, "evaluator", {}, {"passed": False}, 0, status="failed")


async def test_ten_evaluator_retries_set_off_the_alert_once_at_a_price_that_makes_them_cost(caplog):
    """The roadmap's test. At the price of a frontier model — $3 and $15 a million — a build is 6 cents."""
    db, trip_id = Rows(), uuid.uuid4()

    with (
        patch.object(settings, "LLM_PRICES", {LARGE: (3.00, 15.00)}),
        caplog.at_level(logging.WARNING, logger="src.ai.utils.run_logger"),
    ):
        await retries(db, trip_id, 10)

    alerts = [record.getMessage() for record in caplog.records if "[COST ALERT]" in record.getMessage()]
    assert len(alerts) == 1  # when the total passed the limit — not again on each of the eight rows after
    assert alerts[0] == (
        f"[COST ALERT] trip {trip_id}: the models have cost $0.1224 over 2 runs — "
        "past the $0.10 one trip is expected to stay under"
    )
    assert sum(row.cost_usd or 0 for row in db.rows) == pytest.approx(0.612)  # ten builds, in the end


async def test_at_the_prices_the_models_have_ten_retries_cost_three_cents_and_set_off_nothing(caplog):
    """What the roadmap's threshold means for this app: $0.10 is thirty-three itineraries, not ten."""
    db, trip_id = Rows(), uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="src.ai.utils.run_logger"):
        await retries(db, trip_id, 10)
        assert "[COST ALERT]" not in caplog.text
        assert sum(row.cost_usd or 0 for row in db.rows) == pytest.approx(0.0306)

        # a limit set to what a runaway looks like here does catch them
        again = Rows()
        with patch.object(settings, "LLM_COST_ALERT_USD", 0.02):
            await retries(again, trip_id, 10)
    assert caplog.text.count("[COST ALERT]") == 1 and "past the $0.02" in caplog.text


async def test_a_limit_below_a_cent_is_said_as_it_was_set(caplog):
    """Found on the live run: with the limit at $0.002 the log said "past the $0.00"."""
    with (
        patch.object(settings, "LLM_COST_ALERT_USD", 0.002),
        caplog.at_level(logging.WARNING, logger="src.ai.utils.run_logger"),
    ):
        await retries(Rows(), uuid.uuid4(), 1)

    assert "the models have cost $0.0031 over 1 run — past the $0.002 one trip is expected" in caplog.text
    assert [run_logger._dollars(limit) for limit in (0.10, 0.02, 1, 2.5)] == ["$0.10", "$0.02", "$1.00", "$2.50"]


async def test_the_alert_is_about_one_trip_can_be_switched_off_and_never_fails_a_run(caplog):
    db = Rows()

    with patch.object(settings, "LLM_PRICES", {LARGE: (3.00, 15.00)}), caplog.at_level(logging.WARNING):
        for _ in range(5):  # five trips, one build each: 6 cents a trip, 30 in all
            await log_agent_run(Rows(), uuid.uuid4(), "itinerary_builder", {}, {}, 1, usage=asked(LARGE, *BUILD))
        assert "[COST ALERT]" not in caplog.text

        with patch.object(settings, "LLM_COST_ALERT_USD", 0):
            await retries(db, uuid.uuid4(), 4)
        assert "[COST ALERT]" not in caplog.text and db.asked == 0  # off: nothing is even added up

        broken = Rows()
        broken.scalar = AsyncMock(side_effect=ConnectionError("the database went away"))
        row = await log_agent_run(broken, uuid.uuid4(), "itinerary_builder", {}, {}, 1, usage=asked(LARGE, *BUILD))
    assert row.cost_usd == pytest.approx(0.0612)  # the row is written; only the adding-up was skipped
    assert "Could not add up the model cost" in caplog.text and "[COST ALERT]" not in caplog.text


# ── GET /admin/cost-summary: who may ask, and for what ─────────────────────


async def test_the_cost_summary_needs_a_signed_in_user_and_a_real_date():
    from app.db.session import get_db
    from app.main import app

    user_id = uuid.uuid4()
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(id=user_id))

    async def database():
        yield session

    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/admin/cost-summary")).status_code == 401
            signed_in = {"Authorization": f"Bearer {create_access_token(str(user_id))}"}
            bad_date = await client.get("/admin/cost-summary?since=last-week", headers=signed_in)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert bad_date.status_code == 422


def test_the_alert_limit_and_the_price_table_are_what_the_docs_say():
    assert settings.LLM_COST_ALERT_USD == 0.10  # the roadmap's
    assert PRICES == {LARGE: (0.15, 0.75), SMALL: (0.10, 0.50), GEMINI: (0.30, 2.50)}
    assert run_logger.settings is settings
