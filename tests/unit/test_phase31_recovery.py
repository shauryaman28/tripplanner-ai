"""Phase 31 — what the app does when something it depends on stops: the fixes the chaos exercises led to.

Four things were broken on purpose while a trip was being planned (docs/chaos-report.md), and the
app as it was failed three of them in ways a traveller would have met:

    the tool server killed        every later search failed, with an empty reason, until a restart
    the builder's model silent    the trip said "planning" for as long as the process lived
    Redis away                    a plan in flight took as long as the outage; a new one was a bare 500

These hold what was changed. The tool server itself is killed for real in
tests/integration/test_phase31_chaos_integration.py, and the whole of it on a running stack by
scripts/chaos.py.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR, ErrorData
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import main
from app.api.routes import trips
from src.ai.agents import preference_extractor
from src.ai.agents.evaluator import MAX_EVALUATOR_RETRIES
from src.ai.builder import builder
from src.ai.mcp_client import client
from src.ai.mcp_server.models import ToolError
from src.ai.orchestrator import orchestrator
from src.ai.utils.failures import can_retry, failure_words

# ── The tool server dies ───────────────────────────────────────────────────


def answered(value) -> MagicMock:
    """What a tool call comes back as when the server answered."""
    return MagicMock(isError=False, structuredContent={"result": value})


def a_session(*, raises=None, answers=None) -> AsyncMock:
    session = AsyncMock()
    session.call_tool.side_effect = raises
    if raises is None:
        session.call_tool.return_value = answered(answers)
    return session


@pytest.mark.parametrize(
    "how_it_shows",
    [
        anyio.ClosedResourceError(),  # written to after the pipe was closed — and it has no message at all
        anyio.BrokenResourceError(),
        anyio.EndOfStream(),
        BrokenPipeError(),
        McpError(
            ErrorData(code=CONNECTION_CLOSED, message="Connection closed")
        ),  # the library, to every waiting request
        client._ServerGone("its pipe closed before it answered"),
    ],
)
async def test_a_call_that_finds_the_tool_server_gone_starts_another_and_asks_again(how_it_shows):
    dead, new = a_session(raises=how_it_shows), a_session(answers=[{"name": "A hotel"}])
    with (
        patch.object(client, "get_session", AsyncMock(side_effect=[dead, new])),
        patch.object(client, "_forget", AsyncMock()) as forget,
    ):
        found = await client.call_tool("search_hotels", {"destination": "Goa"})

    assert found == [
        {"name": "A hotel"}
    ]  # the traveller's search is answered: a search changes nothing, so it is asked twice
    forget.assert_awaited_once_with(dead)  # …and the dead one is let go of, so that nothing else is sent to it
    new.call_tool.assert_awaited_once()


async def test_a_tool_server_that_dies_again_is_not_asked_a_third_time():
    first, second = a_session(raises=anyio.ClosedResourceError()), a_session(raises=anyio.BrokenResourceError())
    sessions = AsyncMock(side_effect=[first, second, a_session(answers=[])])
    with patch.object(client, "get_session", sessions), patch.object(client, "_forget", AsyncMock()) as forget:
        told = await client.call_tool("search_flights", {"origin": "DEL"})

    assert isinstance(told, ToolError) and told.code == "CONNECTION_REFUSED" == client.SERVER_GONE
    assert told.error == "The tool server could not be reached."  # a reason, where there was "MCP client exception: "
    assert sessions.await_count == 2 and forget.await_count == 2


async def test_a_tool_server_that_will_not_start_is_the_same_thing_to_its_caller():
    will_not = AsyncMock(side_effect=RuntimeError("the tool server did not start"))
    with patch.object(client, "get_session", will_not), patch.object(client, "_forget", AsyncMock()):
        told = await client.call_tool("get_attractions", {"destination": "Goa"})
    assert told.code == "CONNECTION_REFUSED" and will_not.await_count == 2

    once, then = AsyncMock(side_effect=[OSError("no such file: python"), a_session(answers=[1, 2])]), None
    with patch.object(client, "get_session", once), patch.object(client, "_forget", AsyncMock()):
        then = await client.call_tool("get_attractions", {"destination": "Goa"})
    assert then == [1, 2]  # it started the second time: the search is answered


@pytest.mark.parametrize(
    "something_else",
    [ValueError("bad arguments"), McpError(ErrorData(code=INTERNAL_ERROR, message="the tool raised")), TimeoutError()],
)
async def test_an_error_that_is_not_the_server_dying_is_not_taken_for_it(something_else):
    """Asking again is for a server that is gone. A tool that failed would only fail again — and be run twice."""
    session = a_session(raises=something_else)
    with patch.object(client, "get_session", AsyncMock(return_value=session)) as sessions:
        with patch.object(client, "_forget", AsyncMock()) as forget:
            told = await client.call_tool("search_hotels", {})

    assert told.code == "MCP_CLIENT_ERROR" and type(something_else).__name__ in told.error
    assert sessions.await_count == 1 and session.call_tool.await_count == 1
    forget.assert_not_awaited()  # the tool server is alive: it is kept


async def test_only_the_call_that_holds_the_dead_session_lets_go_of_it():
    """A plan's searches run side by side and find the same server dead together: the second to say so must not
    take the *new* one down."""
    dead, new = MagicMock(), MagicMock()
    old_server, new_server = MagicMock(session=dead, stop=AsyncMock()), MagicMock(session=new, stop=AsyncMock())
    with patch.object(client, "_session", dead), patch.object(client, "_client_context", old_server):
        await client._forget(dead)
        assert client._session is None and client._client_context is None
        old_server.stop.assert_awaited_once()  # its pipes and what is left of its process: put away

        client._session, client._client_context = new, new_server  # somebody has started another meanwhile
        await client._forget(dead)  # …and a second call reports the same death
        assert client._session is new and client._client_context is new_server
        new_server.stop.assert_not_awaited()
        await client._forget(None)  # a server that never started is nobody's to forget
        assert client._session is new


async def test_a_call_is_not_waited_for_once_the_servers_pipe_has_closed():
    """Seen: a server killed while it was being started for three calls at once. Two were told; the third had
    sent its request a moment after the library stopped listening, and waited for ever."""
    never = asyncio.Event()

    async def no_answer(*_args, **_kwargs):
        await never.wait()

    session = MagicMock(call_tool=no_answer)
    server = MagicMock(session=session, gone=asyncio.Event())
    with patch.object(client, "_client_context", server):
        asking = asyncio.ensure_future(client._ask(session, "search_hotels", {"input": {}}, {}))
        await asyncio.sleep(0.02)
        assert not asking.done()  # the server is alive, and slow: it is waited for
        server.gone.set()  # its pipe closes
        with pytest.raises(client._ServerGone, match="closed before it answered"):
            await asyncio.wait_for(asking, 1)

        with pytest.raises(client._ServerGone, match="already closed"):  # …and nothing more is sent down it
            await client._ask(session, "search_hotels", {"input": {}}, {})
    assert client._gone(client._ServerGone("x"))


async def test_a_call_that_is_answered_is_answered_as_before():
    session = a_session(answers={"temp": 31})
    server = MagicMock(session=session, gone=asyncio.Event())
    with patch.object(client, "_client_context", server):
        assert (await client._ask(session, "get_weather", {"input": {"q": 1}}, {"trip_id": "t"})).structuredContent == {
            "result": {"temp": 31}
        }
    session.call_tool.assert_awaited_once_with("get_weather", arguments={"input": {"q": 1}}, meta={"trip_id": "t"})
    # a session nobody here is watching the pipe of (the tests' own, mostly) is simply asked
    other = a_session(answers=1)
    await client._ask(other, "get_weather", {"input": {}}, {})
    other.call_tool.assert_awaited_once_with("get_weather", arguments={"input": {}})


async def test_every_start_of_the_tool_server_is_counted():
    """One is the process starting. Any more is a tool server that died — the number an alert is put on."""

    def starts() -> float:
        return REGISTRY.get_sample_value("mcp_tool_server_starts_total")

    class Started:
        async def start(self):
            return MagicMock()

        async def stop(self):
            pass

    before = starts()
    with (
        patch.object(client, "_session", None),
        patch.object(client, "_client_context", None),
        patch.object(client, "_ToolServer", Started),
    ):
        first = await client.get_session()
        assert await client.get_session() is first and starts() - before == 1  # kept: not started for every call
        await client._forget(first)
        assert await client.get_session() is not first and starts() - before == 2


async def test_a_tool_server_that_will_not_start_leaves_nothing_behind():
    class WillNot:
        async def start(self):
            raise RuntimeError("the tool server did not start")

    with (
        patch.object(client, "_session", None),
        patch.object(client, "_client_context", None),
        patch.object(client, "_ToolServer", WillNot),
    ):
        with pytest.raises(RuntimeError):
            await client.get_session()
        assert client._session is None and client._client_context is None  # the next call tries from scratch


def test_the_traveller_is_told_the_search_service_stopped_and_that_asking_again_may_help():
    gone = {"code": "CONNECTION_REFUSED", "error": "The tool server could not be reached."}
    assert failure_words(gone) == "The search service stopped answering. Try again in a moment."
    assert can_retry(gone)
    # …and never the program's own words for it, which is what the page showed: "MCP client exception: "
    assert failure_words({"code": "MCP_CLIENT_ERROR", "error": "MCP client exception: "}) == (
        "The search could not be completed."
    )


# ── A plan with nothing to build from, and a plan with less than it wanted ──

GONE = {"code": "CONNECTION_REFUSED", "error": "The tool server could not be reached."}


async def said_at_the_end(node, state: dict) -> str:
    heard: list[dict] = []

    async def publish(event: dict) -> None:
        heard.append(event)

    with patch.object(orchestrator, "_set_trip_status", AsyncMock()), patch.object(orchestrator, "_log", AsyncMock()):
        await node({**state, "publish_fn": publish})
    (failed,) = [event for event in heard if event.get("event") == "planning_failed"]
    return failed["error"]


async def test_searches_that_could_not_be_run_are_not_reported_as_a_destination_with_nothing_in_it():
    could_not = {"destination": "Udaipur", "flight_error": GONE, "hotel_error": GONE, "activities_error": GONE}
    assert await said_at_the_end(orchestrator.nothing_found_node, could_not) == orchestrator.SEARCHES_DID_NOT_RUN
    assert "Udaipur" not in orchestrator.SEARCHES_DID_NOT_RUN and "try again" in orchestrator.SEARCHES_DID_NOT_RUN

    # one search that ran and found nothing: the trip really has nothing to plan from
    found_nothing = {
        **could_not,
        "hotel_error": {"code": "NO_RESULTS", "error": "No hotels within ₹900/night in Udaipur."},
    }
    assert "were found for Udaipur" in await said_at_the_end(orchestrator.nothing_found_node, found_nothing)
    # two failed and the third was never asked: not "none of the searches could be run"
    assert "were found for Udaipur" in await said_at_the_end(
        orchestrator.nothing_found_node, {"destination": "Udaipur", "flight_error": GONE, "hotel_error": GONE}
    )
    # and a destination the tools cannot use still says so itself
    unknown = {**could_not, "activities_error": {"code": "NOT_FOUND", "error": "Could not find a place called Xyzzy."}}
    assert await said_at_the_end(orchestrator.nothing_found_node, unknown) == "Could not find a place called Xyzzy."


def test_a_plan_made_without_a_search_says_which_and_why():
    state = {
        "flights": [{"price_inr": 9000}],
        "hotel_error": GONE,
        "activities_error": {"code": "NO_RESULTS", "error": "Nothing to do was found near Leh."},
        "flight_error": None,
    }
    assert orchestrator.missing_searches(state) == [
        {
            "search": "hotel_agent",
            "what": "a place to stay",
            "why": "The search service stopped answering. Try again in a moment.",
            "retryable": True,
        },
        {
            "search": "activities_agent",
            "what": "things to do",
            "why": "Nothing to do was found near Leh.",
            "retryable": False,
        },
    ]
    assert orchestrator.missing_searches({"flights": [], "hotels": []}) == []  # nothing failed: nothing is said


async def test_the_label_is_the_planners_to_write_never_the_models():
    """`missing` is in the saved itinerary. A model that wrote one into its reply would be believed."""
    reply = json.dumps(
        {
            "days": [{"day": 1, "date": "2026-12-01", "morning": {"activity": "Aguada Fort", "cost": 0}}],
            "total_cost": 0,
            "currency": "INR",
            "missing": [{"search": "flight_agent", "what": "flights", "why": "the model says so"}],
        }
    )
    with patch.object(builder, "_call_llm", AsyncMock(return_value=reply)):
        draft = await builder.build_itinerary(
            {"destination": "Goa", "start_date": "2026-12-01", "end_date": "2026-12-01"},
            [],
            [],
            [{"name": "Aguada Fort"}],
        )
    assert isinstance(draft, builder.ItineraryDraft), draft
    assert draft.missing is None and "missing" in builder.ItineraryDraft.model_fields


# ── The model that writes the itinerary does not answer ────────────────────

TRIP = {"destination": "Goa", "start_date": "2026-12-01", "end_date": "2026-12-03", "group_size": 2}


async def test_a_build_whose_model_never_answers_is_over_at_the_deadline():
    async def never(*_args, **_kwargs):
        await asyncio.sleep(3600)

    began = time.monotonic()
    with patch.object(builder, "_call_llm", never), patch.object(builder, "BUILD_DEADLINE_S", 0.05):
        told = await builder.build_itinerary(TRIP, [], [{"name": "H"}], [])

    assert isinstance(told, builder.BuilderError) and told.code == "LLM_TIMEOUT"
    assert time.monotonic() - began < 1.0  # at the deadline — it used to be never
    assert "did not answer" in told.error


async def test_the_clients_own_timeout_is_a_timeout_too_and_anything_else_is_not():
    class APITimeoutError(
        Exception
    ):  # what Groq's client raises: by its name, so that the client is not imported for it
        pass

    with patch.object(builder, "_call_llm", AsyncMock(side_effect=APITimeoutError("Request timed out."))):
        assert (await builder.build_itinerary(TRIP, [], [{"name": "H"}], [])).code == "LLM_TIMEOUT"
    with patch.object(builder, "_call_llm", AsyncMock(side_effect=RuntimeError("429 rate limit"))):
        told = await builder.build_itinerary(TRIP, [], [{"name": "H"}], [])
    assert told.code == "LLM_ERROR" and "429" in told.error


def test_the_deadline_leaves_the_rest_of_a_plan_inside_the_roadmaps_minute():
    """A trip whose builder hangs is to be marked failed within 60 s of being asked for. Before the builder
    come intent parsing (4.6 s at its slowest), the searches (7.5 s) and the local tips (8 s at most)."""
    assert builder.BUILD_DEADLINE_S <= 30 and builder.BUILD_READ_TIMEOUT_S < builder.BUILD_DEADLINE_S
    assert builder.BUILD_DEADLINE_S >= 3 * 8.4  # …and three times the slowest build that was ever measured


async def test_the_builders_client_is_made_with_a_timeout_and_one_more_try():
    made = {}

    class Groq:
        def __init__(self, **options) -> None:
            made.update(options)

        async def ainvoke(self, _messages):
            return MagicMock(content="{}")

    with patch("langchain_groq.ChatGroq", Groq):
        await builder._call_llm("system", "user")
    # no timeout meant *no* timeout: the wrapper hands the SDK None, and the SDK then waits for ever
    assert made["timeout"] == builder.BUILD_READ_TIMEOUT_S and made["max_retries"] == 1

    made.clear()
    with patch("langchain_groq.ChatGroq", Groq), patch.object(preference_extractor.settings, "GROQ_API_KEY", "a-key"):
        await preference_extractor._call_llm("system", "user")
    assert made["timeout"] == preference_extractor.EXTRACTION_TIMEOUT_S <= 10 and made["max_retries"] == 0


def test_a_build_that_timed_out_is_not_built_again_and_one_that_failed_otherwise_is():
    timed_out = {
        "builder_error": {"code": "LLM_TIMEOUT", "error": "…"},
        "draft_itinerary": None,
        "evaluator_retry_count": 0,
    }
    assert orchestrator.route_after_evaluator(timed_out) == "failed"  # the next build would wait just as long
    failed = {**timed_out, "builder_error": {"code": "LLM_ERROR", "error": "429"}}
    assert orchestrator.route_after_evaluator(failed) == "retry"
    assert orchestrator.route_after_evaluator({**failed, "evaluator_retry_count": MAX_EVALUATOR_RETRIES}) == "failed"


async def test_the_traveller_is_told_the_plan_could_not_be_written_not_what_the_client_raised():
    timed_out = {"builder_error": {"code": "LLM_TIMEOUT", "error": "The model did not answer within 30 s."}}
    with patch.object(orchestrator.metrics, "itinerary_build"):
        said = await said_at_the_end(orchestrator.builder_failed_node, timed_out)
        assert said == orchestrator._BUILDER_WORDS["LLM_TIMEOUT"] and "did not answer in time" in said
        unreachable = {"builder_error": {"code": "LLM_ERROR", "error": "LLM call failed: Error code: 500 - {...}"}}
        assert "Error code" not in await said_at_the_end(orchestrator.builder_failed_node, unreachable)
        # a plan that failed its checks still says which
        checks = {"builder_error": {"code": "DATA_SCOPE_VIOLATION", "error": "Day 2 uses a hotel that was not found."}}
        assert (
            await said_at_the_end(orchestrator.builder_failed_node, checks) == "Day 2 uses a hotel that was not found."
        )


async def test_what_the_run_wrote_down_as_its_reason_is_what_a_page_loaded_later_is_told():
    """The stream said "the model did not answer in time"; GET /status, to a page that had missed it, said
    "The model did not answer within 30 s." — the builder's note to itself."""
    logged = {}

    async def log(_state, name, **row):
        logged[name] = row

    timed_out = {"builder_error": {"code": "LLM_TIMEOUT", "error": "The model did not answer within 30 s."}}
    with (
        patch.object(orchestrator, "_set_trip_status", AsyncMock()),
        patch.object(orchestrator, "_log", log),
        patch.object(orchestrator.metrics, "itinerary_build"),
    ):
        await orchestrator.builder_failed_node(timed_out)

    output = logged["builder_failed"]["output"]
    assert output["reason"] == orchestrator._BUILDER_WORDS["LLM_TIMEOUT"]
    assert output["builder_error"] == timed_out["builder_error"]  # and what the client said is kept beside it
    source = (Path(trips.__file__)).read_text()
    asked = source[source.index('latest_run.get("builder_failed")') :][:600]
    assert asked.index('output.get("reason")') < asked.index('output.get("builder_error")')  # the words first


# ── Redis is away ──────────────────────────────────────────────────────────


def a_redis(publish) -> MagicMock:
    return MagicMock(publish=AsyncMock(side_effect=publish))


async def test_a_run_does_not_wait_on_a_redis_that_is_not_there():
    """Dozens of pieces of an itinerary are published while it is written. Each one tried against a dead Redis
    cost a second or two — and a plan in flight took as long as the outage."""
    redis = a_redis(RedisConnectionError("Error -2 connecting to upstash:6379. Name or service not known."))
    publish = trips._Publisher(redis, uuid.uuid4())

    for seq in range(40):
        await publish({"event": "builder_token", "seq": seq})  # never raises: a dead Redis does not fail a run

    assert redis.publish.await_count == 1 and publish.dropped == 40  # one found it dead; the rest were not tried


async def test_the_events_that_end_a_run_are_always_tried():
    redis = a_redis(RedisConnectionError("gone"))
    publish = trips._Publisher(redis, uuid.uuid4())
    await publish({"agent": "flight_agent", "status": "completed"})
    await publish({"event": "planning_complete", "status": "completed"})
    await publish({"event": "planning_failed", "status": "failed"})
    assert redis.publish.await_count == 3  # Redis may have come back a second ago: the page should hear this


async def test_publishing_begins_again_once_redis_is_back(caplog):
    answers: list = [RedisTimeoutError("timed out"), 1, 1]
    redis = a_redis(
        lambda *_: (_ for _ in ()).throw(answers.pop(0)) if isinstance(answers[0], Exception) else answers.pop(0)
    )
    trip_id = uuid.uuid4()
    publish = trips._Publisher(redis, trip_id)
    with patch.object(trips._Publisher, "QUIET_S", 0.05), caplog.at_level("WARNING"):
        await publish({"event": "builder_token", "seq": 0})
        await publish({"event": "builder_token", "seq": 1})  # within the quiet: not tried
        assert redis.publish.await_count == 1
        await asyncio.sleep(0.06)
        await publish({"event": "builder_token", "seq": 2})
        await publish({"event": "builder_token", "seq": 3})

    assert redis.publish.await_count == 3 and publish.dropped == 0
    channel, sent = redis.publish.await_args.args
    assert channel == f"trip:{trip_id}:events" and json.loads(sent) == {"event": "builder_token", "seq": 3}
    said = [record.getMessage() for record in caplog.records]
    assert sum("cannot be reached" in line for line in said) == 1  # said once, not for each of forty pieces
    assert any("Redis is back: 2 event(s)" in line for line in said)


async def test_one_publish_is_given_two_seconds_and_no_more():
    async def slow(*_args):
        await asyncio.sleep(3600)

    publish = trips._Publisher(MagicMock(publish=slow), uuid.uuid4())
    began = time.monotonic()
    with patch.object(trips._Publisher, "TRY_S", 0.05):
        await publish({"event": "builder_token", "seq": 0})
    assert time.monotonic() - began < 1.0 and publish.dropped == 1
    assert trips._Publisher.TRY_S == 2.0 and trips._Publisher.QUIET_S == 5.0


async def test_a_redis_that_goes_away_as_a_run_begins_does_not_leave_the_trip_planned_by_nobody():
    """The trip is marked "planning" and committed; then two things are written to Redis; then the run is
    started. Redis going between the first and the last left a trip nobody was planning — and nobody could."""
    trip = MagicMock(id=uuid.uuid4(), status=trips.TripStatus.PENDING)
    db, started = MagicMock(commit=AsyncMock()), []

    def spawned(run):
        started.append(run)
        run.close()

    with (
        patch.object(trips, "save_current_run", AsyncMock(side_effect=RedisConnectionError("gone"))),
        patch.object(trips, "spawn", spawned),
    ):
        await trips._start_run(trip, db, MagicMock(), AsyncMock())

    assert trip.status == trips.TripStatus.PLANNING and db.commit.await_count == 1
    assert len(started) == 1  # and somebody is planning it


async def test_redis_being_away_is_answered_as_something_that_passes():
    for lost in (RedisConnectionError("Error 111 connecting to redis:6379. Connection refused."), RedisTimeoutError()):
        answer = await main.redis_is_away_handler(MagicMock(), lost)
        body = json.loads(answer.body)
        assert answer.status_code == 503 and answer.headers["retry-after"] == "5"
        assert body["error"]["code"] == "SERVICE_UNAVAILABLE" and "try again shortly" in body["detail"]
        assert "6379" not in answer.body.decode()  # where Redis is, is nobody's business
    handlers = main.app.exception_handlers
    assert handlers[RedisConnectionError] is handlers[RedisTimeoutError] is main.redis_is_away_handler
    assert ResponseError not in handlers  # a command Redis refused is a bug, and stays a 500


def test_no_planning_run_is_waited_for_for_ever():
    assert 120 <= trips.RUN_DEADLINE_S <= 600  # a plan takes 10 to 16 s; a search may wait a minute for a rate limit
    assert "took too long" in trips.TOOK_TOO_LONG and "start it again" in trips.TOOK_TOO_LONG
