"""Phase 26 — LangSmith tracing: every run of the planner a trace, every step a span, found by its trip.

What a trace looks like is asserted on what the planner *uploads*. The client
is the app's own (tracing.make_client), the tracing library is the real one,
and its HTTP session goes nowhere: `FakeLangSmith` (tests/fakes.py) answers
every request in this process and puts the runs it receives back together
(src/ai/trace_tree.py). Nothing leaves the machine, and nothing is mocked
between the planner and the request it would have sent.

The planner's model calls are answered by stand-in models that go through
LangChain (`models_answer`), so a model call is a span with tokens here, as it
is on the real stack.

The roadmap's acceptance, in order: one planning run is a complete trace; a
re-planning branch is a distinct span; evaluator retries are numbered spans;
filtering by trip_id returns only that trip's traces.
"""

import contextvars
import json
import logging
import threading
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import langsmith as library
import pytest
from langsmith import utils
from langsmith.run_helpers import get_tracing_context

from app.core.config import Settings
from src.ai import tracing
from src.ai.agents.flight_agent import FlightAgent
from src.ai.mcp_client.client import call_tool
from src.ai.orchestrator import orchestrator
from src.ai.orchestrator.orchestrator import OrchestratorAgent
from src.ai.trace_tree import SERVER_INFO, Runs, Span, render
from tests.fakes import (
    FakeLangSmith,
    FakeProviders,
    flight,
    memory_cache,
    models_answer,
    network_stubs,
    tool_server,
    traced,
)

START = date.today() + timedelta(days=30)
TRIP = {
    "destination": "Goa",
    "start_date": str(START),
    "end_date": str(START + timedelta(days=2)),
    "budget": 60_000.0,
    "group_size": 2,
    "interests": ["history"],
    "raw_input": "A relaxed long weekend",
}
USER = uuid.uuid4()
FLIGHTS = {"origin": "DEL", "destination": "Goa", "date": str(START), "budget": 50_000.0}

# The graph's nodes in the order a plan that goes straight through visits them. (Phase 36 added `language`
# and `localise`: an English plan passes through both as if they were not there, but they are its steps.)
STRAIGHT_THROUGH = [
    "language",
    "intent_parsing",
    "apply_preferences",
    "run_flight",
    "budget_decision",
    "hotel_activities",
    "build_itinerary",
    "evaluate",
    "localise",
    "persist",
    "merge",
    "extract_preferences",
]


async def plan(trip_id: uuid.UUID | None = None, flight_tool=None, **changes) -> tuple[uuid.UUID, dict]:
    """One planning run of TRIP on the stubbed tools. Returns (the trip's id, the run's result)."""
    trip_id = trip_id or uuid.uuid4()
    with network_stubs(flight_tool=flight_tool or (lambda *_: [flight(8_200.0)])), models_answer():
        result = await OrchestratorAgent().run({**TRIP, **changes}, trip_id=trip_id)
    return trip_id, result


def one(traces: list[Span]) -> Span:
    assert len(traces) == 1, [trace.name for trace in traces]
    return traces[0]


# ── One planning run, one complete trace ───────────────────────────────────


async def test_a_planning_run_is_one_trace_with_a_span_for_every_step():
    with traced() as langsmith:
        trip_id, result = await plan()
        trace = one(langsmith.traces(trip_id))

    assert result["draft_itinerary"]
    assert trace.name == "plan trip" and trace.parent_id is None
    assert trace.names() == STRAIGHT_THROUGH  # every LangGraph node, in the order it ran
    # each search agent is named, under the node that ran it
    assert trace.find("run_flight")[0].names() == ["flight_agent"]
    assert {"hotel_agent", "activities_agent", "destination_intelligence"} <= set(
        trace.find("hotel_activities")[0].names()
    )
    # a span has what went in, what came out and how long it took
    for span in trace.walk():
        assert span.started is not None and span.ended is not None and span.duration_ms >= 0, span.name
        assert span.error is None, (span.name, span.error)
    build = trace.find("build_itinerary")[0]
    assert build.inputs["destination"] == "Goa" and build.outputs["draft_itinerary"]["days"]
    assert trace.project == "tests"


async def test_a_model_call_is_a_span_with_its_tokens_and_the_steps_above_it_add_them_up():
    with traced() as langsmith:
        trip_id, _ = await plan()
        trace = one(langsmith.traces(trip_id))

    calls = [span for span in trace.walk() if span.run_type == "llm"]
    asked_by = [next(span.name for span in trace.walk() if call in span.children) for call in calls]
    assert sorted(asked_by) == ["build_itinerary", "destination_intelligence", "intent_parsing"]
    assert all(call.own_tokens > 0 for call in calls)
    build = trace.find("build_itinerary")[0]
    assert build.tokens == build.children[0].own_tokens  # a step's tokens are those of the model call inside it
    assert trace.tokens == sum(call.own_tokens for call in calls)  # the trace's total is its model calls, once each
    assert trace.find("budget_decision")[0].tokens == 0  # a step that asked no model


async def test_every_span_is_tagged_with_its_trip_and_its_user():
    """Metadata set once, on the root, is on every span beneath it — so a filter finds the whole run."""
    with traced() as langsmith, patch.object(orchestrator, "_user_of", AsyncMock(return_value=USER)):
        trip_id, _ = await plan()
        trace = one(langsmith.traces(trip_id))

    spans = list(trace.walk())
    assert len(spans) > 20
    for span in spans:
        assert span.metadata["trip_id"] == str(trip_id), span.name
        assert span.metadata["user_id"] == str(USER), span.name
    assert (trace.metadata["kind"], trace.metadata["turn"]) == ("plan", 1)
    assert f"trip:{trip_id}" in trace.tags and "plan" in trace.tags


async def test_filtering_by_trip_returns_only_that_trips_traces():
    with traced() as langsmith:
        goa, result = await plan()
        jaipur, _ = await plan(destination="Jaipur")
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            await OrchestratorAgent().refine("targeted_hotel", result, "A nicer hotel", trip_id=goa, turn=2)

        of_goa, of_jaipur = langsmith.traces(goa), langsmith.traces(jaipur)
        everything = langsmith.traces()

    assert [trace.name for trace in of_goa] == ["plan trip", "refine trip: targeted_hotel"]
    assert [trace.name for trace in of_jaipur] == ["plan trip"]
    assert len(everything) == 3
    assert all(span.metadata["trip_id"] == str(goa) for trace in of_goa for span in trace.walk())
    assert langsmith.traces(uuid.uuid4()) == []  # a trip that was never planned has none


# ── Branches ───────────────────────────────────────────────────────────────


async def test_every_trace_goes_to_the_one_project_though_tracing_was_switched_on_somewhere_else():
    """Tracing is switched on once, as the app starts. A plan runs later, in a request's own task.

    Found on the first run against LangSmith itself. Left to find the project, LangChain — which
    starts the trace of a plan — looks in the context of the task it is running in and then in
    the process environment, never at what `langsmith.configure` was told: every plan went to a
    project called "default", and every change to a plan (a trace this project opens itself) to
    the right one. A trip's filter then found the changes and not the plan.
    """
    server = FakeLangSmith()
    startup = threading.Thread(target=tracing.use, args=(server.client, "the-trips-project"))
    startup.start()
    startup.join()  # another thread, as the app's startup is another task: nothing it set is in this one's context
    try:
        assert get_tracing_context()["project_name"] is None  # what a request finds when it looks: nothing
        trip_id, result = await plan()
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            agent = OrchestratorAgent()
            await agent.refine("targeted_hotel", result, "A nicer hotel", trip_id=trip_id, turn=2)
            await agent.retry_search("activities_agent", result, trip_id=trip_id, turn=3)
        traces = server.traces(trip_id)
    finally:
        tracing.use(None)

    assert [t.name for t in traces] == ["plan trip", "refine trip: targeted_hotel", "retry trip: activities_agent"]
    assert {trace.project for trace in traces} == {"the-trips-project"}
    # …and not one span of any of them anywhere else: the steps, the model calls, the tools
    assert {span.project for trace in traces for span in trace.walk()} <= {"the-trips-project", None}
    assert server.traces() == traces  # nothing was traced on its own, into some other project


async def test_a_trace_is_handed_its_project_and_none_is_left_to_be_found():
    """Each library can find a project for a trace by itself — each in places of its own, which is what went wrong.

    So nothing is left to be found. Here the tracing library has been made to forget the project it was
    told, and every kind of trace still goes to ours: the one LangChain starts (a plan), the one this
    project opens (a change), and a span that has no trace to belong to.
    """
    with traced(project="named-outright") as langsmith:
        contextvars.copy_context().run(lambda: library.configure(project_name=None))  # process-wide: forgotten
        trip_id, result = await plan()
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            await OrchestratorAgent().refine("targeted_hotel", result, "A nicer hotel", trip_id=trip_id, turn=2)
        with tracing.span("a span on its own") as alone:
            alone.end(outputs={})
        traces = langsmith.traces()

    assert [trace.name for trace in traces] == ["plan trip", "refine trip: targeted_hotel", "a span on its own"]
    assert {trace.project for trace in traces} == {"named-outright"}


def test_switching_tracing_on_leaves_nothing_behind_in_the_callers_own_context():
    """`langsmith.configure` also sets values only the calling task sees. Left in place they would make whoever
    switched tracing on — a test — see more than a request ever does, which is how the bug above stayed hidden."""
    tracing.use(FakeLangSmith().client, "a-project")
    try:
        seen = get_tracing_context()
        assert (seen["project_name"], seen["enabled"], seen["client"]) == (None, None, None)
        assert utils.tracing_is_enabled() is True  # on all the same: for the whole process
    finally:
        tracing.configure(settings_like())  # and off, the way the app says it
    assert get_tracing_context()["enabled"] is None and utils.tracing_is_enabled() is False


def dear_until_a_stop_is_allowed(_name: str, params: dict):
    """₹11,000 direct — 55% of a ₹20,000 budget, so the budget check asks for cheaper flights; ₹6,000 with a stop."""
    return [flight(6_000.0 if params.get("max_stops", 1) >= 2 else 11_000.0)]


async def test_a_replanning_branch_is_a_distinct_span():
    with traced() as langsmith:
        trip_id, result = await plan(flight_tool=dear_until_a_stop_is_allowed, budget=20_000.0)
        trace = one(langsmith.traces(trip_id))

    assert result["draft_itinerary"]
    # the run went through the flight search and the budget check twice…
    assert trace.names()[:7] == [
        "language",
        "intent_parsing",
        "apply_preferences",
        "run_flight",
        "budget_decision",
        "run_flight",
        "budget_decision",
    ]
    decisions = [span.outputs["budget_decision"]["decision"] for span in trace.find("budget_decision")]
    assert decisions == ["replan", "continue"]
    # …and the second search is not just a second "run_flight": it is the re-plan, by name
    first, second = trace.find("run_flight")
    assert first.names() == ["flight_agent"]
    (branch,) = second.children
    assert branch.name == "re-plan 1: cheaper flights"
    assert branch.metadata["replan_attempt"] == 1
    assert branch.inputs == {"budget_cap": 13_000.0, "max_stops": 2}
    assert branch.outputs == {"flights_found": 1, "error": None}
    assert branch.names() == ["flight_agent"]  # the search it made is inside it
    assert len(trace.find("re-plan 1: cheaper flights")) == 1


async def test_evaluator_retries_are_numbered_spans():
    """A builder that keeps naming a place nobody found: sent back three times, then the run gives up."""

    def invents_a_place(_system: str, user_prompt: str) -> str:
        day = {"day": 1, "date": str(START), "morning": {"activity": "Atlantis", "cost": 0}, "hotel": None}
        return json.dumps({"days": [day], "total_cost": 0, "currency": "INR"})

    trip_id = uuid.uuid4()
    with traced() as langsmith:
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer(large=invents_a_place):
            result = await OrchestratorAgent().run(dict(TRIP), trip_id=trip_id)
        trace = one(langsmith.traces(trip_id))

    assert result["draft_itinerary"] is None
    retries = [span for span in trace.walk() if span.name.startswith("evaluator retry")]
    assert [span.name for span in retries] == ["evaluator retry 1", "evaluator retry 2", "evaluator retry 3"]
    assert [span.metadata["evaluator_retry"] for span in retries] == [1, 2, 3]
    assert all(span.metadata["agent"] == "activities_agent" for span in retries)
    assert retries[0].inputs["builder_error"]["code"] == "DATA_SCOPE_VIOLATION"  # why it was sent back
    assert retries[0].outputs == {"searched_again": "activities_agent", "found": 3}
    assert retries[0].names() == ["activities_agent"]  # the search the retry repeated is inside it
    # each is under the node that made it, and the loop is there to be read: build, check, retry, build, …
    assert [parent.name for parent in trace.children if any(r in parent.children for r in retries)] == [
        "retry_dispatch"
    ] * 3
    assert trace.names().count("build_itinerary") == 4 and trace.names()[-1] == "builder_failed"
    routes = [span.outputs.get("output") for span in trace.find("route_after_evaluator")]
    assert routes == ["retry", "retry", "retry", "failed"]  # the branch taken each time, in the trace too


# ── Runs that are not one pass through the graph ───────────────────────────


async def test_a_refinement_and_a_retried_search_are_traces_of_their_own_with_the_graphs_steps():
    with traced() as langsmith:
        trip_id, result = await plan()
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            agent = OrchestratorAgent()
            await agent.refine("targeted_hotel", result, "A nicer hotel", trip_id=trip_id, turn=2)
            await agent.retry_search("activities_agent", result, trip_id=trip_id, turn=3)
            await agent.refine("add_day", result, "One more day", trip_id=trip_id, turn=4)
        _, refine, retry, longer = langsmith.traces(trip_id)

    assert refine.name == "refine trip: targeted_hotel"
    assert (refine.metadata["kind"], refine.metadata["detail"], refine.metadata["turn"]) == (
        "refine",
        "targeted_hotel",
        2,
    )
    assert refine.inputs == {"request": "A nicer hotel"}
    assert refine.names() == ["language", "hotel_agent", "build_itinerary", "evaluate", "localise", "persist", "merge"]
    built = refine.find("build_itinerary")[0]
    assert built.children[0].run_type == "llm"  # the model call, under its step
    # a step says what went in and what came out, as a node of the graph does
    assert built.inputs["destination"] == "Goa" and built.outputs["draft_itinerary"]["days"]
    assert refine.find("evaluate")[0].outputs["evaluator_verdict"]["passed"] is True
    assert refine.outputs["itinerary_id"] is None  # no database here: the run's own summary, as its output

    assert retry.name == "retry trip: activities_agent" and retry.metadata["kind"] == "retry"
    assert retry.names()[:2] == ["language", "activities_agent"]

    # "add a day" is a whole pass through the graph again: the same steps as a plan, named as what it is
    assert longer.name == "refine trip: add_day" and longer.metadata["kind"] == "refine"
    assert longer.names()[:3] == STRAIGHT_THROUGH[:3]


async def test_a_tool_call_is_a_span_of_the_trace_it_is_made_in_and_no_trace_on_its_own():
    """Through the real MCP client and server: what was asked of the tool, and what came back."""
    trip_id = uuid.uuid4()
    with traced() as langsmith, memory_cache():
        async with tool_server(FakeProviders()):
            with tracing.root("plan trip", "plan", trip_id, USER, turn=1):
                found = await FlightAgent().run(dict(FLIGHTS))
            # a tool called outside any run — a trip's caches being warmed — is nobody's trace
            assert not isinstance(await call_tool("search_flights", FLIGHTS), dict)
        trace = one(langsmith.traces())

    assert found["error"] is None
    (tool,) = trace.find("mcp:search_flights")
    assert tool.run_type == "tool"
    assert tool.inputs == {**FLIGHTS, "passengers": 1}  # what the agent asked for: the search, as it sent it
    assert [f["price_inr"] for f in tool.outputs["result"]] == [4200.0, 6100.0]
    assert tool.metadata["trip_id"] == str(trip_id)
    path = [trace.name, "flight_agent", "search_flights", "mcp:search_flights"]
    assert trace.find("flight_agent")[0].find("search_flights")[0].names() == ["mcp:search_flights"], path


async def test_a_tool_that_fails_is_a_span_that_failed_and_says_why():
    """The client never raises, so the span has to be told — or a failed search looks like one that worked."""
    providers = FakeProviders()
    providers.turn_away["duffel"] = [401]
    with traced() as langsmith, memory_cache():
        async with tool_server(providers):
            with tracing.root("plan trip", "plan", uuid.uuid4()):
                found = await FlightAgent().run(dict(FLIGHTS))
        trace = one(langsmith.traces())
        (tool,) = trace.find("mcp:search_flights")

    assert tool.outputs == {"error": "Duffel API error: HTTP 401", "code": "DUFFEL_ERROR"}
    assert tool.error == "DUFFEL_ERROR: Duffel API error: HTTP 401"
    assert "✗ DUFFEL_ERROR" in render(trace)
    # the search was answered — with an error the agent handles — so nothing above the call failed
    assert found["error"]["code"] == "DUFFEL_ERROR"
    assert [span.name for span in trace.walk() if span.error] == ["mcp:search_flights"]


# ── What leaves the machine ────────────────────────────────────────────────


async def test_what_is_uploaded_has_no_live_objects_and_the_key_only_in_a_header():
    async def publish(_event: dict) -> None:
        pass

    async def done(_state: dict) -> None:
        pass

    trip_id = uuid.uuid4()
    with traced() as langsmith:
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            await OrchestratorAgent().run(dict(TRIP), trip_id=trip_id, publish_fn=publish, on_complete=done)
        trace = one(langsmith.traces(trip_id))
        sent = langsmith.uploaded()

    for span in trace.walk():
        assert not tracing.RUNTIME_KEYS & set(span.inputs), (span.name, sorted(span.inputs))
        assert not tracing.RUNTIME_KEYS & set(span.outputs), (span.name, sorted(span.outputs))
    assert "publish_fn" not in sent
    assert "on_complete" not in sent
    assert "test_what_is_uploaded" not in sent  # the two callbacks' own names: neither was written out as text
    assert trace.find("build_itinerary")[0].inputs["trip_id"] == str(trip_id)  # data stays: only live objects go
    # the key authenticates the upload; it is never part of what is uploaded
    assert FakeLangSmith.KEY not in sent
    uploads = [headers for method, _path, headers, _body in langsmith.requests if method == "POST"]
    assert uploads and all(headers["x-api-key"] == FakeLangSmith.KEY for headers in uploads)
    assert {path for method, path, _h, _b in langsmith.requests if method == "POST"} == {"/runs/batch"}


@pytest.mark.parametrize("compressed", [False, True], ids=["multipart", "compressed multipart"])
async def test_sent_the_way_langsmith_itself_is_sent_it_is_the_same_trace_without_the_live_objects(compressed):
    """The local stand-in asks for JSON batches. Left to itself the library sends multipart — and compressed
    multipart, by another road again, to a server that says it takes it, as a current LangSmith does."""

    async def publish(_event: dict) -> None:
        pass

    trip_id = uuid.uuid4()
    with traced(FakeLangSmith(multipart=True, compressed=compressed)) as langsmith:
        with network_stubs(flight_tool=lambda *_: [flight(8_200.0)]), models_answer():
            await OrchestratorAgent().run(dict(TRIP), trip_id=trip_id, publish_fn=publish)
        trace = one(langsmith.traces(trip_id))
        sent = langsmith.uploaded()

    posted = [(path.split("?")[0], headers.get("Content-Encoding")) for method, path, headers, _ in langsmith.requests]
    assert {path for path, _ in posted[1:]} == {"/runs/multipart"}  # after the GET /info: not one JSON batch
    # compressed for a server that takes it — though the first runs can leave before the library has set that up
    assert ("zstd" in {encoding for _, encoding in posted}) is compressed
    assert trace.name == "plan trip" and trace.names() == STRAIGHT_THROUGH
    assert trace.tokens > 0 and trace.find("build_itinerary")[0].tokens > 0
    assert all(span.metadata.get("trip_id") == str(trip_id) for span in trace.walk())
    assert trace.find("build_itinerary")[0].inputs["destination"] == "Goa"
    for span in trace.walk():
        assert not tracing.RUNTIME_KEYS & (set(span.inputs) | set(span.outputs)), span.name
    assert "publish_fn" not in sent and "on_complete" not in sent and FakeLangSmith.KEY not in sent


def test_scrub_takes_out_what_is_not_data_and_nothing_else():
    state = {"destination": "Goa", "db": object(), "publish_fn": print, "on_complete": None, "trip_id": "T"}

    assert tracing.scrub(state) == {"destination": "Goa", "trip_id": "T"}
    assert tracing.scrub("a string") == "a string" and tracing.scrub(None) is None
    assert "db" in state  # the run's own state is not touched


# ── On, off, and what cannot switch it ─────────────────────────────────────


def settings_like(**changes) -> SimpleNamespace:
    values = {
        "LANGCHAIN_TRACING_V2": False,
        "LANGCHAIN_API_KEY": "",
        "LANGCHAIN_PROJECT": "tripplanner-ai",
        "LANGCHAIN_ENDPOINT": "",
        "APP_ENV": "development",
    }
    return SimpleNamespace(**{**values, **changes})


def test_tracing_is_off_unless_it_is_switched_on_and_given_a_key(caplog):
    made = []
    client = FakeLangSmith().client

    def make(api_key, api_url=None, session=None):
        made.append((api_key, api_url))
        return client

    shipped = Settings.model_fields
    assert shipped["LANGCHAIN_TRACING_V2"].default is False and shipped["LANGCHAIN_API_KEY"].default == ""

    with patch.object(tracing, "make_client", make), caplog.at_level(logging.INFO, logger="src.ai.tracing"):
        assert tracing.configure(settings_like()) is False  # the default
        assert tracing.configure(settings_like(LANGCHAIN_API_KEY="a-key")) is False  # a key is not a switch
        assert tracing.configure(settings_like(LANGCHAIN_TRACING_V2=True)) is False  # a switch is not a key
        assert "LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is empty" in caplog.text
        on_in_tests = settings_like(LANGCHAIN_TRACING_V2=True, LANGCHAIN_API_KEY="a-key", APP_ENV="test")
        assert tracing.configure(on_in_tests) is False  # the test stacks never trace
        assert made == [] and not tracing.enabled()

        both = settings_like(LANGCHAIN_TRACING_V2=True, LANGCHAIN_API_KEY="a-key", LANGCHAIN_ENDPOINT="http://x:1984")
        assert tracing.configure(both) is True
        assert tracing.enabled() and made == [("a-key", "http://x:1984")]
        assert "tracing is on — project 'tripplanner-ai', sent to http://x:1984" in caplog.text
        assert "a-key" not in caplog.text  # the key is never logged

        assert tracing.configure(settings_like()) is False and not tracing.enabled()  # and off again


async def test_the_app_configures_tracing_as_it_starts_and_sends_the_last_traces_as_it_stops():
    """Nothing else calls them: left out of the app's startup, tracing would be off for ever and say nothing."""
    from app import main

    done: list = []
    quiet = {"init_redis": AsyncMock(return_value=AsyncMock()), "_recover_after_restart": AsyncMock()}

    def app_running(**closing):
        patches = {"_show_app_logs": MagicMock(), "close_session": AsyncMock(), "close_redis": AsyncMock()}
        return patch.multiple(main, **{**patches, **quiet, **closing})

    with (
        patch.object(main.tracing, "configure", side_effect=lambda given: done.append(("configure", given))),
        patch.object(main.tracing, "shutdown", side_effect=lambda: done.append("the last traces sent")),
    ):
        with app_running():
            async with main.lifespan(main.app):
                assert done == [("configure", main.settings)]  # before a request is served, from the app's settings
        assert done[1:] == ["the last traces sent"]

        # …and they are sent even when something else fails to close
        done.clear()
        with app_running(close_redis=AsyncMock(side_effect=RuntimeError("redis is gone"))):
            with pytest.raises(RuntimeError, match="redis is gone"):
                async with main.lifespan(main.app):
                    pass
        assert done[1:] == ["the last traces sent"]


def test_a_shell_that_says_trace_cannot_switch_tracing_on(monkeypatch):
    """The settings decide. A `LANGSMITH_TRACING=true` left in a terminal must not upload anyone's trips."""
    from langchain_core.tracers.context import _tracing_v2_is_enabled

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "a-key-from-the-shell")
    utils.get_env_var.cache_clear()

    try:
        library.configure(enabled=None)  # a process that has just started: nobody has told the library anything
        assert utils.tracing_is_enabled() and _tracing_v2_is_enabled()  # left to itself, it does what the shell says

        tracing.configure(settings_like())  # our settings: off
        assert not utils.tracing_is_enabled() and not _tracing_v2_is_enabled()
    finally:
        utils.get_env_var.cache_clear()


async def test_with_tracing_off_nothing_is_made_and_nothing_is_sent():
    with traced() as langsmith:  # on for a moment and off again: the client is still there, and no longer in use
        pass

    _, result = await plan()
    with tracing.root("refine trip", "refine", uuid.uuid4()) as trace, tracing.span("evaluator retry 1") as span:
        span.end(outputs={"ok": True})
        trace.end(outputs={})

    assert result["draft_itinerary"]
    assert langsmith.traces() == []
    assert [method for method, *_ in langsmith.requests if method != "GET"] == []  # not one upload
    assert tracing.run_config("plan trip", "plan", uuid.uuid4())["run_name"] == "plan trip"  # a config nobody reads


async def test_a_tracing_service_that_is_down_never_fails_a_plan():
    with traced(FakeLangSmith(down=True)) as langsmith:
        trip_id, result = await plan()
        assert langsmith.traces(trip_id) == []  # nothing got through…

    assert result["draft_itinerary"]["days"]  # …and the traveller has a plan
    assert any(method == "POST" for method, *_ in langsmith.requests)  # it did try


def test_shutdown_waits_for_the_last_traces_but_not_for_ever(caplog):
    stuck = threading.Event()
    never_done = SimpleNamespace(flush=lambda: stuck.wait(30))  # a queue nothing empties

    with (
        patch.object(tracing, "_client", never_done),
        patch.object(tracing, "FLUSH_SECONDS", 0.2),
        caplog.at_level(logging.WARNING, logger="src.ai.tracing"),
    ):
        tracing.shutdown()  # returns: it must not hold a server's shutdown

    stuck.set()
    assert "the last traces were not sent within 0 s" in caplog.text
    tracing.shutdown()  # with tracing off there is nothing to wait for

    sent = []
    with patch.object(tracing, "_client", SimpleNamespace(flush=lambda: sent.append("flushed"))):
        tracing.shutdown()
    assert sent == ["flushed"]  # and when it can, it does send them


# ── Reading what was uploaded (src/ai/trace_tree.py) ───────────────────────


ORDER = {"1": "1", "2": "1.2", "3": "1.3", "4": "1.2.4"}  # each run's place in the tree, as the library sends it


def record(run_id: str, name: str, parent: str | None = None, **more) -> dict:
    return {"id": run_id, "name": name, "parent_run_id": parent, "dotted_order": ORDER[run_id], **more}


def test_runs_are_merged_and_hung_under_their_parents_in_the_order_they_started():
    runs = Runs()
    runs.take(
        {
            "post": [
                record("1", "plan trip", start_time="2026-10-11T10:00:00", extra={"metadata": {"trip_id": "T"}}),
                record("3", "evaluate", "1", start_time="2026-10-11T10:00:02"),
                record("2", "build_itinerary", "1", start_time="2026-10-11T10:00:01"),
                record("4", "ChatGroq", "2", run_type="llm", start_time="2026-10-11T10:00:01"),
            ],
            "patch": [
                # the record of a run's end repeats little and says None for the rest: it adds, it never wipes
                record("1", None, end_time="2026-10-11T10:00:09.500000", outputs={"done": True}, extra=None),
                record(
                    "4",
                    "ChatGroq",
                    "2",
                    end_time="2026-10-11T10:00:05",
                    outputs={"generations": [[{"message": {"kwargs": {"usage_metadata": {"total_tokens": 4210}}}}]]},
                ),
                record("2", "build_itinerary", "1", end_time="2026-10-11T10:00:05", error="ValueError: no plan\nmore"),
            ],
        }
    )
    runs.take_one({"name": "a record without an id is nobody's"})

    (trace,) = runs.traces("T")
    assert len(runs) == 4
    assert trace.names() == ["build_itinerary", "evaluate"]  # by when they started, not by when they arrived
    assert (trace.duration_ms, trace.tokens, trace.outputs) == (9_500, 4_210, {"done": True})
    assert trace.find("evaluate")[0].duration_ms is None  # it never ended
    assert runs.traces("another trip") == []
    drawn = render(trace).splitlines()
    assert drawn[:3] == [
        "plan trip   trip T                                                     9.5 s    4,210 tokens",
        "├─ build_itinerary                                                     4.0 s    4,210 tokens  ✗ ValueError: no plan",
        "│  └─ ChatGroq  [llm]                                                  4.0 s    4,210 tokens",
    ]
    assert drawn[3].startswith("└─ evaluate") and drawn[3].endswith("…")  # still running: no time to show


def test_the_local_stand_in_answers_the_tracing_library_as_a_server_must():
    """A batch config with a key missing stops the library's sending thread — and with it every trace, silently."""
    assert set(SERVER_INFO["batch_ingest_config"]) >= {
        "use_multipart_endpoint",
        "size_limit",
        "size_limit_bytes",
        "scale_up_qsize_trigger",
        "scale_up_nthreads_limit",
        "scale_down_nempty_trigger",
    }
    assert SERVER_INFO["batch_ingest_config"]["use_multipart_endpoint"] is False
