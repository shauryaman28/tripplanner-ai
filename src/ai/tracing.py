"""LangSmith tracing — every run of the planner as a trace, filterable by trip (Phase 26).

Switched on by two settings together, `LANGCHAIN_TRACING_V2=true` and a
`LANGCHAIN_API_KEY`; `configure()` is called once, when the app starts. Off,
nothing here does anything and nothing leaves the machine.

On, a run looks like this in LangSmith (or in scripts/trace_receiver.py, the
local stand-in):

    plan trip                                  ← the root: metadata trip_id, user_id, turn, kind
      intent_parsing
        ChatGoogleGenerativeAI                 ← a model call: prompt, reply, tokens
      apply_preferences
      run_flight
        flight_agent
          search_flights
            mcp:search_flights                 ← a tool call: what was asked, what came back
      budget_decision
      run_flight
        re-plan 1: cheaper flights             ← a branch taken, as a span of its own
      …
      evaluate
      retry_dispatch
        evaluator retry 1                      ← numbered: the loop is countable at a glance
      build_itinerary
        itinerary_builder
          ChatGroq

Three kinds of span, one tree:

- Every LangGraph node and every model call is a span without any code here:
  LangChain traces its own runs once tracing is on.
- `root()` opens the trace of a run that is not one graph invocation — a
  refinement or a retried search calls the graph's nodes one by one — and
  `step()` makes each of those calls the span the graph would have made it.
- `span()` marks what a node's name alone does not say: which retry this is,
  that this flight search is a re-plan, which tool was called.

The trip and the user go on the root as metadata, and LangSmith hands metadata
down to every span beneath it: "trip_id" in its filter finds every decision
made for one trip, and nothing else.

What is uploaded is what the planner saw and wrote — the destination, the
dates, the travellers' names and interests, the plan, the prompts. The live
objects a run carries in its state (the database session, the callbacks) are
taken out (`scrub`); so is nothing else. Tracing is the operator's choice, made
in `.env`: the environment a shell happens to have (`LANGSMITH_TRACING`) cannot
switch it on, and cannot switch it off.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import langsmith

logger = logging.getLogger(__name__)

# What a run's state holds that is not data: never uploaded.
RUNTIME_KEYS = frozenset({"db", "publish_fn", "on_complete"})
FLUSH_SECONDS = 5.0  # how long shutdown waits for the last traces to be sent

_client: langsmith.Client | None = None


def scrub(data: Any) -> Any:
    """A run's inputs or outputs as they are uploaded: without the live objects its state carries."""
    return {key: value for key, value in data.items() if key not in RUNTIME_KEYS} if isinstance(data, dict) else data


def enabled() -> bool:
    return _client is not None


def configure(settings: Any) -> bool:
    """Switch tracing on or off as the settings say. Returns whether it is on.

    On needs both the switch and a key. `APP_ENV=test` keeps it off whatever
    `.env` says — the test stacks plan dozens of trips and none of them belongs
    in anyone's project. Off is said to LangSmith's library too, so that a
    `LANGSMITH_TRACING=true` left in a shell does not trace behind our back.
    """
    global _client
    off = None
    if not settings.LANGCHAIN_TRACING_V2:
        off = "LANGCHAIN_TRACING_V2 is not true"
    elif settings.APP_ENV == "test":
        off = "APP_ENV is test"
    elif not settings.LANGCHAIN_API_KEY:
        off = "LANGCHAIN_TRACING_V2 is true but LANGCHAIN_API_KEY is empty"
        logger.warning("[TRACING] off: %s. Set a key in .env, or switch tracing off.", off)

    if off:
        _client = None
        langsmith.configure(enabled=False)
        logger.info("[TRACING] LangSmith tracing is off (%s)", off)
        return False

    use(make_client(settings.LANGCHAIN_API_KEY, settings.LANGCHAIN_ENDPOINT), settings.LANGCHAIN_PROJECT)
    where = settings.LANGCHAIN_ENDPOINT or "LangSmith"
    logger.info("[TRACING] LangSmith tracing is on — project %r, sent to %s", settings.LANGCHAIN_PROJECT, where)
    return True


def make_client(api_key: str, api_url: str | None = None, session: Any = None) -> langsmith.Client:
    """A LangSmith client that uploads a run without the live objects in its state.

    `api_url`: another LangSmith than the default one; `session`: the HTTP
    session to send through — the tests hand in one that goes nowhere.
    """
    return langsmith.Client(
        api_key=api_key, api_url=api_url or None, session=session, hide_inputs=scrub, hide_outputs=scrub
    )


def use(client: langsmith.Client | None, project: str = "tripplanner-ai") -> None:
    """Trace through this client from now on; None switches tracing off. `configure` and the tests end up here."""
    global _client
    _client = client
    if client is None:
        langsmith.configure(enabled=False)
    else:
        langsmith.configure(client=client, enabled=True, project_name=project)


def shutdown() -> None:
    """Send what is still waiting — for a few seconds at most.

    Traces are uploaded by a background thread; a trace finished a moment
    before the app stops would otherwise be lost. The wait is bounded because
    the library's own flush is not: it waits for a queue that nothing empties
    once its thread has died.
    """
    if _client is None:
        return
    sender = threading.Thread(target=_client.flush, daemon=True)
    sender.start()
    sender.join(FLUSH_SECONDS)
    if sender.is_alive():
        logger.warning("[TRACING] the last traces were not sent within %.0f s", FLUSH_SECONDS)


# ── What a run is tagged with ──────────────────────────────────────────────


def _labels(kind: str, trip_id: Any, user_id: Any, turn: int | None, **more: Any) -> tuple[dict, list[str]]:
    metadata = {"trip_id": str(trip_id) if trip_id else None, "user_id": str(user_id) if user_id else None}
    metadata.update(kind=kind, turn=turn, **more)
    metadata = {key: value for key, value in metadata.items() if value is not None}
    tags = [kind] + ([f"trip:{trip_id}"] if trip_id else [])
    return metadata, tags


def run_config(name: str, kind: str, trip_id: Any, user_id: Any = None, turn: int | None = None, **more: Any) -> dict:
    """The LangGraph config that makes one graph invocation a trace of its trip.

    `name` is what the trace is called; the metadata is what it is found by.
    Harmless when tracing is off: a config nobody reads.
    """
    metadata, tags = _labels(kind, trip_id, user_id, turn, **more)
    return {"run_name": name, "metadata": metadata, "tags": tags}


# ── Spans ──────────────────────────────────────────────────────────────────


class _NoSpan:
    """What `span()` and `root()` hand out when there is nothing to record."""

    def end(self, **_: Any) -> None:
        pass


@contextmanager
def root(
    name: str,
    kind: str,
    trip_id: Any,
    user_id: Any = None,
    turn: int | None = None,
    inputs: dict | None = None,
    **more: Any,
) -> Iterator[Any]:
    """The trace of a run that is not one graph invocation; everything inside becomes its spans."""
    if not enabled():
        yield _NoSpan()
        return
    metadata, tags = _labels(kind, trip_id, user_id, turn, **more)
    with langsmith.trace(name, inputs=inputs or {}, metadata=metadata, tags=tags) as run:
        yield run


@contextmanager
def span(
    name: str,
    *,
    run_type: str = "chain",
    inputs: dict | None = None,
    metadata: dict | None = None,
    needs_parent: bool = False,
) -> Iterator[Any]:
    """A span inside the trace under way. `span.end(outputs=…)` says what came of it.

    `needs_parent`: record it only as part of a trace, never as a trace of its
    own — a tool call made while a trip's caches are warmed belongs to no run,
    and a project full of one-span traces is no use to anyone.
    """
    if not enabled() or (needs_parent and langsmith.get_current_run_tree() is None):
        yield _NoSpan()
        return
    with langsmith.trace(name, run_type=run_type, inputs=inputs or {}, metadata=metadata) as run:
        yield run


async def step(name: str, node: Callable[[dict], Awaitable[dict]], state: dict) -> dict:
    """Run a graph node outside the graph, as the span the graph would have made of it."""
    with span(name, inputs=state) as run:
        result = await node(state)
        run.end(outputs=result)
        return result
