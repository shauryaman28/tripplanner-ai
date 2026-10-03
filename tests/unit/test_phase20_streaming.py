"""Phase 20 (backend) — the itinerary is streamed while it is written, and a failed search can be retried.

  builder        `on_token` sees every piece of the reply; the reply is still checked whole
  token stream   pieces travel in `builder_token` events: in order, numbered, a few at a time
  graph          a reply that is streamed and then fails its checks is never saved
  retry          one search again for a plan built without it; the rest of the plan is kept
  failures       why a search failed, in the traveller's words, and whether trying again can help
  routes         POST /trips/{id}/retry; GET /status says why a search failed and what run is in flight

No network, no Docker.
"""

import json
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.models.agent_run import AgentRun
from app.models.itinerary import Itinerary
from app.models.trip import Trip, TripStatus
from src.ai.agents.evaluator import MAX_EVALUATOR_RETRIES
from src.ai.builder import builder as builder_module
from src.ai.builder.builder import BuilderError, ItineraryBuilder, ItineraryDraft, build_itinerary
from src.ai.orchestrator import orchestrator as orchestrator_module
from src.ai.orchestrator.orchestrator import (
    _RETRY_REQUESTS,
    RETRY_REFINEMENTS,
    TOKEN_FLUSH_SECONDS,
    OrchestratorAgent,
    _TokenStream,
    build_itinerary_node,
)
from src.ai.utils.conversation import clear_current_run, get_current_run, save_current_run
from src.ai.utils.failures import can_retry, failure_words
from tests.fakes import ATTRACTIONS, HOTELS, STREAM_PIECE, fake_builder_llm, flight, stream_reply

START = date.today() + timedelta(days=30)
META = {"destination": "Goa", "start_date": str(START), "end_date": str(START + timedelta(days=2)), "group_size": 2}
FLIGHTS = [flight(8_200.0, day=str(START))]


def _state(**extra) -> dict:
    return {**META, "flights": FLIGHTS, "hotels": HOTELS, "attractions": ATTRACTIONS, "budget": 50_000.0, **extra}


def _tokens(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("event") == "builder_token"]


class Collector:
    """An `on_token` / `publish_fn` that remembers what it was given."""

    def __init__(self):
        self.seen: list = []

    async def __call__(self, item) -> None:
        self.seen.append(item)


# ── The model call ─────────────────────────────────────────────────────────


class FakeGroq:
    """Stands in for langchain_groq.ChatGroq: a stream of chunks, or one whole reply."""

    # What Groq sends for a reasoning model: reasoning first, with empty content, then the answer.
    chunks = [
        SimpleNamespace(content="", additional_kwargs={"reasoning_content": "The trip is three days…"}),
        SimpleNamespace(content='{"days"', additional_kwargs={}),
        SimpleNamespace(content="", additional_kwargs={"reasoning_content": "…"}),
        SimpleNamespace(content=[{"type": "text", "text": ": []"}], additional_kwargs={}),  # content as blocks
        SimpleNamespace(content="}", additional_kwargs={}),
    ]
    calls: list[str] = []

    def __init__(self, **options):
        self.options = options

    async def ainvoke(self, messages):
        FakeGroq.calls.append("ainvoke")
        return SimpleNamespace(content='{"days": []}')

    async def astream(self, messages):
        FakeGroq.calls.append("astream")
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def groq():
    FakeGroq.calls = []
    with patch("langchain_groq.ChatGroq", FakeGroq):
        yield FakeGroq


@pytest.mark.asyncio
async def test_the_reply_is_streamed_piece_by_piece_and_returned_whole(groq):
    """Roadmap: stream the builder's output token by token. Reasoning is not part of the reply."""
    seen = Collector()
    reply = await builder_module._call_llm("system", "user", seen)
    assert seen.seen == ['{"days"', ": []", "}"]  # the pieces, in order — never an empty one, never the reasoning
    assert reply == '{"days": []}' == "".join(seen.seen)
    assert groq.calls == ["astream"]


@pytest.mark.asyncio
async def test_without_a_listener_the_model_is_asked_for_the_whole_reply(groq):
    assert await builder_module._call_llm("system", "user") == '{"days": []}'
    assert groq.calls == ["ainvoke"]  # nobody to stream to → no stream


# ── The builder ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_hands_over_every_piece_and_still_checks_the_whole_reply():
    seen = Collector()
    with patch("src.ai.builder.builder._call_llm", fake_builder_llm):
        draft = await build_itinerary(META, FLIGHTS, HOTELS, ATTRACTIONS, on_token=seen)

    assert isinstance(draft, ItineraryDraft)
    assert len(seen.seen) > 10 and all(0 < len(piece) <= STREAM_PIECE for piece in seen.seen)
    written = json.loads("".join(seen.seen))  # what was streamed is the reply itself, nothing more or less
    assert [day["morning"]["activity"] for day in written["days"]] == [
        "Fort Aguada",
        "Basilica of Bom Jesus",
        "Explore the area",
    ]
    assert written["total_cost"] == draft.total_cost == 8_200 + 2 * 4_500
    # the stream is the model's raw text; the draft is what the checks made of it (coordinates from the search)
    assert written["days"][0]["morning"]["lat"] is None and draft.days[0].morning.lat == 15.492


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ('{"days": [{"day": 1, "date": "2027-12-10", "morning": {"activity": "Fort Agu', "JSON_PARSE_ERROR"),  # cut off
        ('{"days": "soon", "total_cost": 0}', "SCHEMA_INVALID"),
        (
            json.dumps(
                {
                    "days": [{"day": 1, "date": "2027-12-10", "morning": {"activity": "Atlantis", "cost": 0}}],
                    "total_cost": 8200,
                }
            ),
            "DATA_SCOPE_VIOLATION",
        ),
    ],
)
async def test_a_streamed_reply_that_fails_the_checks_is_an_error_like_any_other(reply, code):
    """What the page was shown while it streamed makes no difference to what the builder accepts."""
    seen = Collector()

    async def writes(_system, _prompt, on_token):
        return await stream_reply(reply, on_token)

    with patch("src.ai.builder.builder._call_llm", writes):
        result = await build_itinerary(META, FLIGHTS, HOTELS, ATTRACTIONS, on_token=seen)

    assert "".join(seen.seen) == reply  # it was streamed in full…
    assert isinstance(result, BuilderError) and result.code == code  # …and rejected all the same


@pytest.mark.asyncio
async def test_a_stream_that_breaks_off_is_an_llm_error():
    async def breaks(_system, _prompt, on_token):
        await on_token('{"days": [')
        raise ConnectionError("stream closed")

    with patch("src.ai.builder.builder._call_llm", breaks):
        result = await build_itinerary(META, FLIGHTS, HOTELS, ATTRACTIONS, on_token=Collector())
    assert isinstance(result, BuilderError) and result.code == "LLM_ERROR"


@pytest.mark.asyncio
async def test_a_two_argument_stand_in_still_works_when_nothing_is_streamed():
    """Tests written before Phase 20 replace `_call_llm` with (system, prompt) functions."""

    async def old_style(_system, _prompt):
        return await fake_builder_llm(_system, _prompt)

    with patch("src.ai.builder.builder._call_llm", old_style):
        result = await ItineraryBuilder().run(META, FLIGHTS, HOTELS, ATTRACTIONS)
    assert result["error"] is None and len(result["draft"]["days"]) == 3


# ── The token stream ───────────────────────────────────────────────────────


@pytest.fixture
def clock():
    """time.monotonic under the test's control."""
    now = SimpleNamespace(value=100.0)
    with patch.object(orchestrator_module.time, "monotonic", lambda: now.value):
        yield now


@pytest.mark.asyncio
async def test_pieces_travel_together_and_in_order(clock):
    """A publish is a round trip to Redis and the model writes hundreds of tokens a second."""
    published = Collector()
    stream = _TokenStream({"publish_fn": published})

    await stream.push('{"da')  # the first piece goes out at once: it says writing has begun
    assert _tokens(published.seen) == [
        {"event": "builder_token", "agent": "itinerary_builder", "token": '{"da', "seq": 0}
    ]

    for piece in ('ys"', ": ", "["):  # written within the same 50 ms…
        clock.value += TOKEN_FLUSH_SECONDS / 10
        await stream.push(piece)
    assert len(published.seen) == 1  # …and held back

    clock.value += TOKEN_FLUSH_SECONDS
    await stream.push('{"day"')
    assert published.seen[1] == {
        "event": "builder_token",
        "agent": "itinerary_builder",
        "token": 'ys": [{"day"',
        "seq": 1,
    }

    await stream.push(": 1")
    await stream.flush()  # the end of the reply: whatever is left goes out
    await stream.flush()  # nothing left → nothing sent
    assert [event["seq"] for event in published.seen] == [0, 1, 2]
    assert "".join(event["token"] for event in published.seen) == '{"days": [{"day": 1'


@pytest.mark.asyncio
async def test_a_dead_redis_does_not_stop_the_build():
    stream = _TokenStream({"publish_fn": AsyncMock(side_effect=ConnectionError("redis is away"))})
    await stream.push("{")
    await stream.flush()  # no exception


@pytest.mark.asyncio
async def test_build_node_publishes_the_reply_as_it_is_written():
    published = Collector()
    with patch("src.ai.builder.builder._call_llm", fake_builder_llm):
        state = await build_itinerary_node(_state(publish_fn=published))

    tokens = _tokens(published.seen)
    assert published.seen == tokens and tokens  # nothing else is published by this node
    assert [event["seq"] for event in tokens] == list(range(len(tokens)))
    assert all(event["agent"] == "itinerary_builder" and event["token"] for event in tokens)
    written = json.loads("".join(event["token"] for event in tokens))
    assert [day["date"] for day in written["days"]] == [day["date"] for day in state["draft_itinerary"]["days"]]
    assert state["builder_error"] is None


@pytest.mark.asyncio
async def test_build_node_does_not_stream_when_nobody_listens():
    with patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder:
        builder.return_value.run = AsyncMock(return_value={"draft": None, "error": None})
        await build_itinerary_node(_state())
        assert builder.return_value.run.await_args.kwargs["on_token"] is None

        await build_itinerary_node(_state(publish_fn=AsyncMock()))
        assert builder.return_value.run.await_args.kwargs["on_token"] is not None


# ── The graph: only a checked itinerary is saved ───────────────────────────


@contextmanager
def _searches(flights=None, hotels=None, attractions=None):
    """The three sub-agents, answering with search results (or an error dict)."""
    answers = {
        "FlightAgent": ("flights", FLIGHTS if flights is None else flights),
        "HotelAgent": ("hotels", HOTELS if hotels is None else hotels),
        "ActivitiesAgent": ("attractions", ATTRACTIONS if attractions is None else attractions),
    }
    with ExitStack() as stack:
        mocks = {}
        for name, (key, answer) in answers.items():
            agent = stack.enter_context(patch(f"src.ai.orchestrator.orchestrator.{name}")).return_value
            failed = isinstance(answer, dict)
            agent.run = AsyncMock(return_value={key: [] if failed else answer, "error": answer if failed else None})
            mocks[name] = agent
        stack.enter_context(patch("src.ai.orchestrator.orchestrator._extract_intent", AsyncMock(return_value={})))
        yield mocks


def _db(trip: Trip | None = None) -> MagicMock:
    """A session that remembers what was added to it."""
    db = MagicMock()
    db.added = []
    db.add = db.added.append
    db.commit, db.refresh, db.rollback, db.scalar = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=trip)
    db.execute = AsyncMock(return_value=MagicMock())
    return db


def _trip(**fields) -> Trip:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "destination": "Goa",
        "start_date": START,
        "end_date": START + timedelta(days=2),
        "budget": 50_000.0,
        "group_size": 2,
        "interests": ["history"],
        "status": "pending",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    return Trip(**{**defaults, **fields})


@pytest.mark.asyncio
async def test_a_run_streams_the_itinerary_before_it_announces_it():
    published = Collector()
    trip = _trip()
    db = _db(trip)
    with (
        _searches(),
        patch("src.ai.builder.builder._call_llm", fake_builder_llm),
        patch("src.ai.orchestrator.orchestrator.spawn", lambda coro: coro.close()),  # no embeddings here
        patch("src.ai.orchestrator.orchestrator.get_trip_user_id", AsyncMock(return_value=None)),
    ):
        result = await OrchestratorAgent().run(_state(), db=db, trip_id=trip.id, publish_fn=published)

    order = [event.get("event") or event["agent"] for event in published.seen]
    assert order[0] == "flight_agent" and set(order[1:3]) == {"hotel_agent", "activities_agent"}
    assert set(order[3:-1]) == {"builder_token"} and order[-1] == "planning_complete"
    saved = [row for row in db.added if isinstance(row, Itinerary)]
    assert len(saved) == 1 and result["itinerary_id"] == saved[0].id
    written = json.loads("".join(event["token"] for event in _tokens(published.seen)))
    assert written["total_cost"] == saved[0].total_cost == 17_200


@pytest.mark.asyncio
async def test_a_streamed_reply_that_is_not_valid_is_never_saved():
    """Roadmap acceptance: the DB write only happens on complete, valid JSON."""
    published = Collector()
    trip = _trip()
    db = _db(trip)
    cut_off = '{"days": [{"day": 1, "date": "' + str(START) + '", "morning": {"activity": "Fort Agu'

    async def writes(_system, _prompt, on_token):
        return await stream_reply(cut_off, on_token)

    with (
        _searches(),
        patch("src.ai.builder.builder._call_llm", writes),
        patch("src.ai.orchestrator.orchestrator.spawn", lambda coro: coro.close()),
        patch("src.ai.orchestrator.orchestrator.get_trip_user_id", AsyncMock(return_value=None)),
    ):
        result = await OrchestratorAgent().run(_state(), db=db, trip_id=trip.id, publish_fn=published)

    assert [row for row in db.added if isinstance(row, Itinerary)] == []  # nothing was written…
    assert result["itinerary_id"] is None and trip.status == TripStatus.FAILED
    # …although every attempt was streamed: the first build and each retry start again from seq 0
    starts = [event for event in _tokens(published.seen) if event["seq"] == 0]
    assert len(starts) == MAX_EVALUATOR_RETRIES + 1
    assert published.seen[-1]["event"] == "planning_failed" and "parse" in published.seen[-1]["error"].lower()
    assert "planning_complete" not in [event.get("event") for event in published.seen]


# ── Retry: one search again ────────────────────────────────────────────────

_PLAN_WITHOUT_HOTEL = {
    "days": [
        {"day": 1, "date": str(START), "morning": {"activity": "Fort Aguada", "cost": 0}, "hotel": None},
        {"day": 2, "date": str(START + timedelta(days=1)), "morning": {"activity": "Baga Beach", "cost": 0}},
        {"day": 3, "date": str(START + timedelta(days=2)), "morning": {"activity": "Explore the area", "cost": 0}},
    ],
    "total_cost": 8_200.0,
    "currency": "INR",
}


def _prior(**extra) -> dict:
    """The state of a run that completed although the hotel search was down."""
    return {
        **_state(hotels=[], interests=["history"]),
        "flight_status": "completed",
        "hotel_status": "failed",
        "activities_status": "completed",
        "hotel_error": {"error": "The hotel search is not answering (HTTP 503).", "code": "PROVIDER_ERROR"},
        "budget_decision": {
            "decision": "continue",
            "reason": "ok",
            "remaining_budget": 41_800.0,
            "flight_cost": 8_200.0,
            "total_budget": 50_000.0,
        },
        "draft_itinerary": _PLAN_WITHOUT_HOTEL,
        "evaluator_retry_count": 1,
        **extra,
    }


@pytest.mark.asyncio
async def test_retry_runs_one_search_again_and_keeps_the_rest_of_the_plan():
    published = Collector()
    with _searches() as agents, patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder:
        draft = {**_PLAN_WITHOUT_HOTEL, "total_cost": 17_200.0}
        draft["days"] = [
            {**day, "hotel": {"name": "Goa Grand", "cost_per_night": 4_500.0} if day["day"] < 3 else None}
            for day in _PLAN_WITHOUT_HOTEL["days"]
        ]
        builder.return_value.run = AsyncMock(return_value={"draft": draft, "error": None})
        result = await OrchestratorAgent().retry_search("hotel_agent", _prior(), publish_fn=published, turn=2)

    agents["HotelAgent"].run.assert_awaited_once()
    agents["FlightAgent"].run.assert_not_called()  # flights and activities are carried forward
    agents["ActivitiesAgent"].run.assert_not_called()
    assert result["hotels"] == HOTELS and result["hotel_status"] == "completed" and result["hotel_error"] is None
    assert result["flights"] == FLIGHTS and result["evaluator_retry_count"] == 0

    offered = builder.return_value.run.await_args.kwargs
    # the builder is shown the plan it is adding to and told to leave the rest alone — no traveller's words involved
    assert offered["trip_meta"]["request"] == _RETRY_REQUESTS["targeted_hotel"]
    assert [day["morning"] for day in offered["trip_meta"]["previous_plan"]] == [
        "Fort Aguada",
        "Baga Beach",
        "Explore the area",
    ]
    # only the places already in the plan are on offer, so the stops cannot be swapped
    assert [a["name"] for a in offered["attractions"]] == ["Fort Aguada", "Baga Beach"]
    assert [event.get("agent") or event.get("event") for event in published.seen] == [
        "hotel_agent",
        "planning_complete",
    ]


@pytest.mark.asyncio
async def test_retry_of_the_activities_search_does_not_reinterpret_the_request():
    """A refinement reads new interests out of the traveller's message; a retry has no message to read."""
    prior = _prior(attractions=[], hotels=HOTELS, activities_status="failed", hotel_status="completed")
    with _searches() as agents, patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder:
        builder.return_value.run = AsyncMock(return_value={"draft": None, "error": {"error": "x", "code": "LLM_ERROR"}})
        await OrchestratorAgent().retry_search("activities_agent", prior, turn=2)
        orchestrator_module._extract_intent.assert_not_called()
    assert agents["ActivitiesAgent"].run.await_args.args[0]["interests"] == ["history"]


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_rebuilds_nothing_and_says_why():
    published = Collector()
    error = {"error": "The hotel search is not answering (HTTP 503).", "code": "PROVIDER_ERROR"}
    db = _db()
    with _searches(hotels=error), patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder:
        builder.return_value.run = AsyncMock()
        result = await OrchestratorAgent().retry_search(
            "hotel_agent", _prior(), db=db, trip_id=uuid.uuid4(), publish_fn=published, turn=2
        )

    builder.return_value.run.assert_not_called()  # the plan would come out as it is
    assert [row for row in db.added if isinstance(row, Itinerary)] == [] and result["itinerary_id"] is None
    assert published.seen[-1] == {
        "event": "planning_failed",
        "agent": "hotel_agent",
        "status": "failed",
        "error": "The hotel search is not answering (HTTP 503).",
    }
    closing = [row for row in db.added if isinstance(row, AgentRun) and row.agent_name == "orchestrator"]
    assert len(closing) == 1 and closing[0].status == "failed"
    assert closing[0].input == {"refinement_type": "targeted_hotel", "retry": "hotel_agent"} and closing[0].turn == 2


@pytest.mark.asyncio
async def test_a_retry_that_fails_again_says_why_in_the_travellers_words():
    published = Collector()
    error = {"error": "Duffel API error: HTTP 503", "code": "DUFFEL_ERROR"}
    with _searches(flights=error), patch("src.ai.orchestrator.orchestrator.ItineraryBuilder"):
        await OrchestratorAgent().retry_search("flight_agent", _prior(), db=_db(), publish_fn=published, turn=2)

    assert {
        "agent": "flight_agent",
        "status": "failed",
        "summary": "The flight provider did not answer.",
        "retryable": True,
    } in published.seen
    assert published.seen[-1]["error"] == "The flight provider did not answer."


@pytest.mark.parametrize(
    ("code", "message", "words", "retry"),
    [
        # a provider that is down or slow may answer on the next try
        ("DUFFEL_ERROR", "Duffel API error: HTTP 503", "The flight provider did not answer.", True),
        ("LITEAPI_ERROR", "LiteAPI error: HTTP 500", "The hotel provider did not answer.", True),
        ("OTM_ERROR", "OpenTripMap API error: HTTP 429", "The attractions provider did not answer.", True),
        ("UNKNOWN_ERROR", "Unexpected error: ReadTimeout", "The search could not be completed.", True),
        (
            "PROVIDER_ERROR",
            "The hotel search is not answering (HTTP 503).",
            "The hotel search is not answering (HTTP 503).",
            True,
        ),
        # the same search would fail the same way: no Retry for these
        (
            "API_NOT_CONFIGURED",
            "Duffel API not configured. Set DUFFEL_ACCESS_TOKEN in .env.",
            "This search is not set up on this server.",
            False,
        ),
        ("NO_RESULTS", "No hotels within ₹1,000/night in Goa.", "No hotels within ₹1,000/night in Goa.", False),
        ("NOT_FOUND", "Could not find a place called Atlantis.", "Could not find a place called Atlantis.", False),
        ("OUTSIDE_COVERAGE", "London is outside India.", "London is outside India.", False),
        ("PAST_DATE", "Departure date is in the past.", "Departure date is in the past.", False),
    ],
)
def test_a_failure_is_told_in_the_travellers_words_and_retried_only_where_that_can_help(code, message, words, retry):
    error = {"code": code, "error": message}
    assert failure_words(error) == words and can_retry(error) is retry


def test_a_failure_with_nothing_to_say_still_says_something():
    assert failure_words(None) == failure_words({}) == "The search could not be completed."
    assert can_retry(None) and can_retry({"error": "?"})


@pytest.mark.asyncio
async def test_a_retry_that_finds_flights_too_dear_ends_in_a_budget_conflict():
    """Flights are found on the retry, and they leave too little: the same check as on the first run."""
    published = Collector()
    prior = _prior(flights=[], hotels=HOTELS, flight_status="failed", hotel_status="completed", budget=10_000.0)
    with _searches() as agents, patch("src.ai.orchestrator.orchestrator.ItineraryBuilder") as builder:
        builder.return_value.run = AsyncMock()
        result = await OrchestratorAgent().retry_search("flight_agent", prior, publish_fn=published, turn=2)

    agents["FlightAgent"].run.assert_awaited_once()
    builder.return_value.run.assert_not_called()
    assert result["budget_decision"]["decision"] == "escalate"
    assert [event.get("event") for event in published.seen][-2:] == ["budget_conflict", "planning_failed"]


def test_every_search_can_be_retried():
    assert set(RETRY_REFINEMENTS) == {"flight_agent", "hotel_agent", "activities_agent"}
    assert set(_RETRY_REQUESTS) == set(RETRY_REFINEMENTS.values())


# ── The note about the run in flight ───────────────────────────────────────


class FakeRedis:
    """get / set / delete on a dict — the three calls the note needs."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.mark.asyncio
async def test_the_run_in_flight_is_noted_and_forgotten():
    redis = FakeRedis()
    assert await get_current_run(redis, "t") is None
    await save_current_run(redis, "t", {"turn": 2, "refinement_type": "targeted_hotel"})
    assert await get_current_run(redis, "t") == {"turn": 2, "refinement_type": "targeted_hotel"}
    assert set(redis.ttl.values()) == {3600}  # a process that dies mid-run must not leave it for ever
    await clear_current_run(redis, "t")
    assert await get_current_run(redis, "t") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redis", [None, AsyncMock(get=AsyncMock(side_effect=ConnectionError("away")))])
async def test_no_redis_no_note_and_no_error(redis):
    assert await get_current_run(redis, "t") is None


@pytest.mark.asyncio
async def test_a_note_that_is_not_a_note_is_ignored():
    redis = FakeRedis()
    redis.data["trip:t:current_run"] = '["not", "a", "run"]'
    assert await get_current_run(redis, "t") is None
    redis.data["trip:t:current_run"] = "{broken"
    assert await get_current_run(redis, "t") is None


# ── Routes ─────────────────────────────────────────────────────────────────


@contextmanager
def _client(trip: Trip | None, runs: list[AgentRun] | None = None, *, redis=None, authenticated: bool = True):
    """App client with auth, the database and Redis overridden; background runs are captured, not executed."""
    from app.api.deps import get_current_user, get_db, get_redis_dep, get_redis_or_none
    from app.main import app

    db = AsyncMock()
    db.add = MagicMock()
    trip_result, runs_result = MagicMock(), MagicMock()
    trip_result.scalar_one_or_none.return_value = trip
    runs_result.scalars.return_value.all.return_value = runs or []
    db.execute = AsyncMock(side_effect=[trip_result, runs_result])
    redis = redis or FakeRedis()
    redis.publish = AsyncMock()

    async def _db():
        yield db

    overrides = {get_db: _db, get_redis_dep: lambda: redis, get_redis_or_none: lambda: redis}
    if authenticated:
        overrides[get_current_user] = lambda: MagicMock(id=trip.user_id if trip else uuid.uuid4())
    app.dependency_overrides.update(overrides)
    try:
        with patch("app.api.routes.trips.spawn") as spawn:
            spawn.side_effect = lambda coro: coro.close()
            yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), spawn, redis
    finally:
        app.dependency_overrides.clear()


def _published(redis) -> dict:
    return json.loads(redis.publish.await_args.args[1])


@pytest.mark.asyncio
async def test_retry_of_one_search_starts_a_targeted_run():
    trip = _trip(status="completed")
    redis = FakeRedis()
    redis.data[f"trip:{trip.id}:planning_state"] = json.dumps(_prior())
    redis.data[f"trip:{trip.id}:conv_history"] = json.dumps([{"role": "user", "content": "beaches", "turn": 1}])

    with _client(trip, redis=redis) as (client, spawn, _):
        with patch("app.api.routes.trips.classify_refinement", AsyncMock()) as classifier:
            response = await client.post(f"/trips/{trip.id}/retry", json={"agent": "hotel_agent"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "retry_started",
        "trip_id": str(trip.id),
        "turn": 2,
        "refinement_type": "targeted_hotel",
        "agent": "hotel_agent",
    }
    classifier.assert_not_called()  # no model decides what a retry is
    spawn.assert_called_once()
    assert trip.status == TripStatus.PLANNING
    started = _published(redis)
    assert (started["event"], started["turn"], started["refinement_type"], started["retry"]) == (
        "planning_started",
        2,
        "targeted_hotel",
        "hotel_agent",
    )
    # what the run is stays readable for a page that loads after that event has gone by
    assert await get_current_run(redis, str(trip.id)) == {
        "turn": 2,
        "refinement_type": "targeted_hotel",
        "retry": "hotel_agent",
    }
    history = json.loads(redis.data[f"trip:{trip.id}:conv_history"])
    assert history[-1] == {"role": "user", "content": "Retry the hotel search.", "turn": 2}


@pytest.mark.asyncio
async def test_retry_of_one_search_needs_the_state_of_the_last_run():
    trip = _trip(status="completed")
    with _client(trip) as (client, spawn, _):
        response = await client.post(f"/trips/{trip.id}/retry", json={"agent": "flight_agent"})
    assert response.status_code == 409 and "too old" in response.json()["error"]["message"]
    spawn.assert_not_called()
    assert trip.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"agent": "hotel_agent"}])
async def test_retry_of_a_failed_trip_plans_it_again_from_what_was_last_written(body):
    """With no plan there is nothing to patch: whichever search is named, the whole trip is planned again."""
    trip = _trip(status="failed", interests=None)
    redis = FakeRedis()
    redis.data[f"trip:{trip.id}:conv_history"] = json.dumps(
        [
            {"role": "user", "content": "Plan a trip to Goa.", "turn": 1},
            {"role": "assistant", "content": "What kinds of activities do you enjoy?", "turn": 1},
            {"role": "user", "content": "beaches and seafood", "turn": 1},
        ]
    )
    with _client(trip, redis=redis) as (client, spawn, _):
        response = await client.post(f"/trips/{trip.id}/retry", **({"json": body} if body is not None else {}))
        assert response.status_code == 200 and response.json()["status"] == "planning_started"
        spawn.assert_called_once()

    assert trip.status == TripStatus.PLANNING and _published(redis)["event"] == "planning_started"
    assert await get_current_run(redis, str(trip.id)) == {"turn": 1}
    # the conversation starts again, from the traveller's own words
    assert json.loads(redis.data[f"trip:{trip.id}:conv_history"]) == [
        {"role": "user", "content": "beaches and seafood", "turn": 1}
    ]


@pytest.mark.asyncio
async def test_retry_with_nothing_to_plan_from_asks_instead_of_assuming():
    """Same rule as POST /plan (#56): no interests and nothing written → a question, not a run."""
    trip = _trip(status="failed", interests=None)
    redis = FakeRedis()
    redis.data[f"trip:{trip.id}:conv_history"] = json.dumps(
        [{"role": "user", "content": "Plan a trip to Goa.", "turn": 1}]  # the stand-in sentence, not the traveller's
    )
    with _client(trip, redis=redis) as (client, spawn, _):
        response = await client.post(f"/trips/{trip.id}/retry")
    assert response.status_code == 200 and response.json()["status"] == "clarification_needed"
    assert "activities" in response.json()["question"]
    spawn.assert_not_called()
    assert trip.status == "failed"


@pytest.mark.asyncio
async def test_retry_is_refused_while_a_run_is_in_flight_or_for_an_unknown_search():
    trip = _trip(status="planning")
    with _client(trip) as (client, spawn, _):
        assert (await client.post(f"/trips/{trip.id}/retry")).status_code == 409
        spawn.assert_not_called()

    trip = _trip(status="completed")
    with _client(trip) as (client, spawn, _):
        response = await client.post(f"/trips/{trip.id}/retry", json={"agent": "weather_agent"})
        assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
        spawn.assert_not_called()


@pytest.mark.asyncio
async def test_retry_needs_a_token_and_the_owner():
    trip = _trip(status="failed")
    with _client(trip, authenticated=False) as (client, _, __):
        assert (await client.post(f"/trips/{trip.id}/retry")).status_code == 401
    with _client(None) as (client, _, __):
        assert (await client.post(f"/trips/{uuid.uuid4()}/retry")).status_code == 404


def _run(
    trip: Trip,
    agent: str,
    status: str = "completed",
    error: str | None = None,
    turn: int = 1,
    code: str = "PROVIDER_ERROR",
) -> AgentRun:
    output = {"error": {"error": error, "code": code}} if error else {"error": None}
    return AgentRun(trip_id=trip.id, agent_name=agent, status=status, output=output, turn=turn)


@pytest.mark.asyncio
async def test_status_says_why_a_search_failed():
    trip = _trip(status="completed")
    runs = [
        _run(trip, "flight_agent", "failed", "The flight search is not answering (HTTP 503)."),
        _run(trip, "hotel_agent"),
        _run(trip, "activities_agent", "failed"),  # failed without a message: listed as failed, no reason to give
        _run(trip, "orchestrator"),
    ]
    with _client(trip, runs) as (client, _, __):
        body = (await client.get(f"/trips/{trip.id}/status")).json()

    assert body["progress"]["agents"] == {
        "flight_agent": "failed",
        "hotel_agent": "completed",
        "activities_agent": "failed",
    }
    assert body["progress"]["errors"] == {"flight_agent": "The flight search is not answering (HTTP 503)."}
    assert body["progress"]["retryable"] == {"flight_agent": True, "activities_agent": True}
    assert body["progress"]["agents_done"] == 1 and body["run"] is None


@pytest.mark.asyncio
async def test_status_words_a_failure_for_the_traveller_and_says_when_a_retry_cannot_help():
    """The live run that found this: a destination typed in Devanagari, which no airport is listed under."""
    trip = _trip(status="completed")
    unknown = "Unknown airport: 'वाराणसी'. Pass an IATA code or add the city to _CITY_IATA in tools.py."
    runs = [
        _run(trip, "flight_agent", "failed", unknown, code="UNKNOWN_DESTINATION"),
        _run(trip, "hotel_agent", "failed", "LiteAPI error: HTTP 502", code="LITEAPI_ERROR"),
        _run(trip, "activities_agent"),
        _run(trip, "orchestrator"),
    ]
    with _client(trip, runs) as (client, _, __):
        progress = (await client.get(f"/trips/{trip.id}/status")).json()["progress"]

    assert progress["errors"] == {
        "flight_agent": "No airport is known by this name. Write the destination in English, or as its airport code, "
        "to include flights.",
        "hotel_agent": "The hotel provider did not answer.",
    }
    assert progress["retryable"] == {"flight_agent": False, "hotel_agent": True}


@pytest.mark.asyncio
async def test_status_forgets_the_reason_once_a_retry_has_succeeded():
    trip = _trip(status="completed")
    runs = [
        _run(trip, "flight_agent"),
        _run(trip, "hotel_agent", "failed", "down"),
        _run(trip, "activities_agent"),
        _run(trip, "orchestrator"),
        _run(trip, "hotel_agent", turn=2),  # the retry
        _run(trip, "orchestrator", turn=2),
    ]
    with _client(trip, runs) as (client, _, __):
        progress = (await client.get(f"/trips/{trip.id}/status")).json()["progress"]
    assert progress["errors"] == {} and progress["agents_done"] == 3


@pytest.mark.asyncio
async def test_status_during_a_targeted_run_keeps_the_searches_it_does_not_repeat():
    """Phase 18's known limit: a page loaded mid-refinement showed all three searches as running."""
    trip = _trip(status="planning")
    redis = FakeRedis()
    await save_current_run(redis, str(trip.id), {"turn": 2, "refinement_type": "targeted_hotel"})
    finished = [
        _run(trip, "flight_agent"),
        _run(trip, "hotel_agent", "failed", "down"),
        _run(trip, "activities_agent"),
        _run(trip, "orchestrator"),
    ]

    with _client(trip, finished, redis=redis) as (client, _, __):  # the hotel search has not answered yet
        body = (await client.get(f"/trips/{trip.id}/status")).json()
    assert body["run"] == {"turn": 2, "refinement_type": "targeted_hotel"}
    assert body["progress"]["agents"] == {
        "flight_agent": "completed",
        "hotel_agent": "pending",  # the one being repeated — its old failure is not this run's
        "activities_agent": "completed",
    }
    assert body["progress"]["errors"] == {}

    with _client(trip, [*finished, _run(trip, "hotel_agent", turn=2)], redis=redis) as (client, _, __):
        progress = (await client.get(f"/trips/{trip.id}/status")).json()["progress"]
    assert progress["agents"]["hotel_agent"] == "completed" and progress["agents_done"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [{"turn": 1}, {"turn": 3, "refinement_type": "full_replan"}, None])
async def test_status_during_a_full_run_starts_again_from_nothing(run):
    trip = _trip(status="planning")
    redis = FakeRedis()
    if run:
        await save_current_run(redis, str(trip.id), run)
    earlier = [_run(trip, name) for name in ("flight_agent", "hotel_agent", "activities_agent", "orchestrator")]

    with _client(trip, [*earlier, _run(trip, "flight_agent", turn=2)], redis=redis) as (client, _, __):
        body = (await client.get(f"/trips/{trip.id}/status")).json()
    assert body["run"] == run
    assert body["progress"]["agents"] == {
        "flight_agent": "completed",
        "hotel_agent": "pending",
        "activities_agent": "pending",
    }


@pytest.mark.asyncio
async def test_the_note_is_cleared_when_the_run_ends_however_it_ends():
    from app.api.routes.trips import _run_orchestrator

    for outcome in ({"itinerary_id": uuid.uuid4()}, {"itinerary_id": None}, RuntimeError("crashed")):
        redis = FakeRedis()
        redis.publish = AsyncMock()
        trip_id = uuid.uuid4()
        await save_current_run(redis, str(trip_id), {"turn": 1})
        run = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        session = _db(_trip())
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("app.api.routes.trips.AsyncSessionLocal", return_value=session):
            await _run_orchestrator(trip_id, redis, run)
        assert await get_current_run(redis, str(trip_id)) is None
