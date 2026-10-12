"""Phase 30 — the stand-ins the load test runs on, and why they cannot reach a deployment.

A load test cannot ask the real models and providers: they allow a handful of requests. So while
it runs, stand-ins answer for them (loadtest/standins/). Two things have to hold, and these tests
hold them:

- **They can only be where the load test put them.** They are not in the image that is deployed,
  nothing in the app can switch them on, the program that puts them in refuses a database that is
  not on this machine, and the stack they run on is never given a real key.
- **The app cannot tell them from the real thing.** A stand-in's plan passes the builder's own
  checks and the evaluator's; a stand-in provider's answer is read by the real tools; a stand-in
  model answers through LangChain, so its call is metered and counted; and each one takes as long
  as the real one was measured to take — a stand-in that answered at once would move the
  bottleneck to wherever the fakes are not.

What they do to a running stack is the other half: `python scripts/loadtest.py smoke` and
docs/load-test-report.md.
"""

import asyncio
import importlib
import json
import re
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml

from app.core.config import Settings
from loadtest.standins import guard, models, providers, seams, timing
from src.ai import llm
from src.ai.agents import destination_intelligence, evaluator, preference_extractor
from src.ai.agents.budget_decision import make_budget_decision
from src.ai.builder import builder
from src.ai.mcp_server import tools
from src.ai.mcp_server.config import MCPSettings
from src.ai.mcp_server.models import AttractionInput, FlightSearchInput, HotelSearchInput, ToolError
from src.ai.utils.run_logger import timed_run
from src.telemetry import metrics
from tests.fakes import _REAL_SEAMS, memory_cache

ROOT = Path(__file__).resolve().parents[2]
DEPLOYED = (ROOT / "src/backend/Dockerfile").read_text()
LOADTEST = (ROOT / "docker/loadtest/Dockerfile").read_text()
OVERLAY = yaml.safe_load((ROOT / "docker/loadtest/compose.yml").read_text().replace("!reset null", "null"))
K6_SCRIPT = (ROOT / "loadtest/k6/traveller.js").read_text()

START = date.today() + timedelta(days=30)
NEON = "postgresql://user:secret@ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
UPSTASH = "rediss://default:secret@apt-mako-12345.upstash.io:6379"


def k6_list(name: str) -> list:
    """A list the k6 script declares (`const NAME = [...]`), read out of the script: one list, not two to keep in step."""
    found = re.search(rf"const {name} = (\[.*?\]);", K6_SCRIPT, flags=re.S)
    assert found, f"the k6 script has no {name}"
    return json.loads(found.group(1).replace("'", '"').replace(",\n]", "\n]"))


@pytest.fixture
def at_once():
    """The stand-ins answer at once: what they say is under test, not how long they take to say it."""
    was, timing.scale = timing.scale, 0
    yield
    timing.scale = was


@pytest.fixture
def stand_in_providers(at_once, no_waiting):
    """The real tools, their cache a dict, their requests answered by the providers' stand-ins."""
    real = tools.httpx
    keys = dict.fromkeys(("DUFFEL_ACCESS_TOKEN", "LITEAPI_API_KEY", "OPENTRIPMAP_API_KEY", "OPENWEATHER_API_KEY"), "k")
    providers.install()
    try:
        with memory_cache() as cache, patch.multiple(tools.mcp_settings, **keys):
            yield cache
    finally:
        tools.httpx = real


def searched(destination: str, interests: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """What the real tools find for a trip when the providers are the stand-ins: (flights, hotels, attractions)."""
    there, back = str(START), str(START + timedelta(days=3))
    found = (
        tools.search_flights(
            FlightSearchInput(
                origin="DEL", destination=destination, date=there, return_date=back, budget=60_000, passengers=2
            )
        ),
        tools.search_hotels(
            HotelSearchInput(destination=destination, check_in=there, check_out=back, budget_per_night=12_000, guests=2)
        ),
        tools.get_attractions(AttractionInput(destination=destination, interests=interests, limit=10)),
    )
    for answer in found:
        assert not isinstance(answer, ToolError), f"{destination} {interests}: {answer}"
    return tuple([item.model_dump() for item in answer] for answer in found)


# ── They can only be where the load test put them ──────────────────────────


def test_the_image_that_is_deployed_has_no_stand_ins_in_it():
    """It copies `src/`, the migrations and their settings — and the stand-ins are none of those."""
    copied = re.findall(r"^COPY (.+)$", DEPLOYED, flags=re.M)

    assert copied == [
        "requirements.txt constraints.txt ./",
        "alembic.ini ./",
        "migrations/ ./migrations/",
        "src/ ./src/",
    ]
    assert not (ROOT / "src" / "loadtest").exists() and (ROOT / "loadtest" / "standins" / "app.py").exists()
    assert "loadtest" not in DEPLOYED
    # …and that is the image a platform is told to build
    assert json.loads((ROOT / "railway.json").read_text())["build"]["dockerfilePath"] == "src/backend/Dockerfile"


def test_nothing_in_the_app_can_switch_them_on():
    """No setting, no flag, no import: the app does not know they exist."""
    knows = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if "frontend" not in path.parts and re.search(r"loadtest|stand_?ins?\b", path.read_text(), flags=re.I)
    ]
    assert not knows, f"the app mentions the load test's stand-ins: {knows}"

    switches = [
        name
        for name in (*Settings.model_fields, *MCPSettings.model_fields)
        if re.search(r"STUB|FAKE|MOCK|STAND|LOADTEST", name)
    ]
    assert not switches, f"a setting that could be one: {switches}"


def test_the_load_tests_image_is_the_deployed_one_serving_another_app():
    (deployed_command,) = re.findall(r"^CMD (.+)$", DEPLOYED, flags=re.M)
    (loadtest_command,) = re.findall(r"^CMD (.+)$", LOADTEST, flags=re.M)

    assert re.search(r"^FROM \$\{DEPLOYED\}$", LOADTEST, flags=re.M)  # built on it, not beside it
    assert re.findall(r"^COPY (.+)$", LOADTEST, flags=re.M) == [
        "loadtest/__init__.py ./loadtest/__init__.py",
        "loadtest/standins/ ./loadtest/standins/",
    ]
    # the same command, word for word, but for what is served
    assert "app.main:app" in deployed_command
    assert loadtest_command == deployed_command.replace("app.main:app", "loadtest.standins.app:app")


def test_the_load_tests_stack_is_never_given_a_real_key():
    given = OVERLAY["services"]["backend"]["environment"]
    keys = ("GOOGLE_API_KEY", "GROQ_API_KEY", "DUFFEL_ACCESS_TOKEN", "LITEAPI_API_KEY", "OPENTRIPMAP_API_KEY")

    for name in (*keys, "OPENWEATHER_API_KEY"):
        assert given[name] == "standin-not-a-key", name  # a word, not ${…}: nothing is read from anywhere
    # every key the production-like stack takes from the repository's .env is one this file replaces
    base = (ROOT / "docker/prodlike/compose.yml").read_text()
    from_env = set(re.findall(r"^      ([A-Z_]+_(?:KEY|TOKEN)): \$\{", base, flags=re.M)) - {"OPS_TOKEN"}
    assert from_env and from_env <= set(given), from_env - set(given)
    # …and the script that starts it does not hand that .env to Docker at all
    runner = (ROOT / "scripts/loadtest.py").read_text()
    assert '"--env-file", SECRETS' in runner and 'ROOT / ".env"' not in runner
    assert OVERLAY["name"] == "tripplanner-prodlike"  # laid over that stack, not a second one beside it


@pytest.mark.parametrize(
    ("url", "here"),
    [
        ("postgresql://tripplanner:pw@pooler:6432/tripplanner?sslmode=require", True),  # another container
        ("rediss://default:pw@upstash-standin:6379?ssl_ca_certs=/certs/ca.crt", True),
        ("postgresql://tripplanner:pw@localhost:5432/tripplanner_db", True),
        ("redis://127.0.0.1:6379", True),
        ("redis://host.docker.internal:6379", True),
        (NEON, False),
        (UPSTASH, False),
        ("postgresql://user:pw@10.0.0.12:5432/db", False),  # an address elsewhere on a network
        ("", False),
        ("not a url", False),
        ("postgresql://user:pw@[::1/db", False),  # an address that cannot even be read is not known to be here
    ],
)
def test_a_database_or_a_redis_with_a_dot_in_its_name_is_somewhere_else(url, here):
    assert guard.is_this_machine(url) is here


def test_the_guard_names_what_is_elsewhere_and_says_what_this_program_is():
    guard.check(DATABASE_URL="postgresql://u:p@pooler:6432/db", REDIS_URL="rediss://default:p@upstash-standin:6379")

    with pytest.raises(guard.NotTheLoadTest) as refused:
        guard.check(DATABASE_URL=NEON, REDIS_URL="rediss://default:p@upstash-standin:6379")
    assert "DATABASE_URL is not on this machine" in str(refused.value)
    assert "stand-ins" in str(refused.value) and "app.main:app" in str(refused.value)
    assert "secret" not in str(refused.value)  # the setting is named; its value is not shown

    with pytest.raises(guard.NotTheLoadTest, match="DATABASE_URL and REDIS_URL are not on this machine"):
        guard.check(DATABASE_URL=NEON, REDIS_URL=UPSTASH)


def serve_the_stand_ins(database_url: str, redis_url: str) -> subprocess.CompletedProcess:
    """Load `loadtest.standins.app` as a web server would, in a process of its own, and say what it left in place."""
    told = {
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "APP_ENV": "test",
        "LOG_FORMAT": "text",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "SENTRY_DSN": "",
        "PYTHONPATH": f"{ROOT / 'src' / 'backend'}:{ROOT}",
        "PATH": "/usr/bin:/bin",
    }
    report = (
        "import sys, langchain_groq\n"
        "import loadtest.standins.app as served\n"
        "from src.ai import llm\n"
        "from src.ai.embeddings import embedder\n"
        "from src.ai.mcp_client import client\n"
        "import app.main\n"
        "started = client.StdioServerParameters(\n"
        "    command=sys.executable, args=['-m', 'src.ai.mcp_server.server'], env={'ITS': 'environment'}, cwd='.')\n"
        "print(served.app is app.main.app, langchain_groq.ChatGroq.__module__, llm.ChatGoogleGenerativeAI.__module__,\n"
        "      embedder._embed_once.__module__, started.args[-1],\n"
        "      (started.command, started.env, str(started.cwd)) == (sys.executable, {'ITS': 'environment'}, '.'))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", report], env=told, capture_output=True, text=True, cwd=ROOT, timeout=120
    )


def test_served_against_a_deployments_database_the_stand_ins_do_not_start():
    """The one accident the other two locks leave: the load test's image, given a deployment's settings."""
    refused = serve_the_stand_ins(NEON, UPSTASH)

    assert refused.returncode != 0
    assert "NotTheLoadTest" in refused.stderr and "DATABASE_URL and REDIS_URL are not on this machine" in refused.stderr
    assert "True" not in refused.stdout  # it stopped before it became the app


def test_served_on_this_machine_it_is_the_app_with_the_four_stand_ins_in():
    served = serve_the_stand_ins(
        "postgresql://tripplanner:pw@pooler:6432/tripplanner", "rediss://default:pw@upstash-standin:6379"
    )

    assert served.returncode == 0, served.stderr[-600:]
    assert (
        served.stdout.split()
        == [
            "True",  # what is served is app.main's app itself, not a copy of it
            "loadtest.standins.models",
            "loadtest.standins.models",
            "loadtest.standins.models",
            "loadtest.standins.tool_server",  # the tool server is started through the module that stands in for its providers…
            "True",  # …by the same interpreter, with the environment and the directory it was to be started with
        ]
    )
    assert (
        "[STAND-INS]" in served.stderr + served.stdout
    )  # and it says so in its log: the load test's script looks for this


def test_nothing_is_stood_in_for_before_the_guard_has_said_yes():
    """Refused after the models had been replaced would be refused too late: the guard first, then the stand-ins."""
    sys.modules.pop("loadtest.standins.app", None)
    with (
        patch.object(guard, "check", side_effect=guard.NotTheLoadTest("DATABASE_URL is not on this machine")),
        patch.object(seams, "install") as install,
        pytest.raises(guard.NotTheLoadTest),
    ):
        importlib.import_module("loadtest.standins.app")

    install.assert_not_called()
    assert "loadtest.standins.app" not in sys.modules  # and what was refused is not there to be served


def test_the_tests_put_the_real_things_back():
    """`seams.installed()` is how these tests use the stand-ins: nothing of them may be left for the next test."""
    import langchain_groq

    from src.ai.embeddings import embedder
    from src.ai.mcp_client import client

    real = (langchain_groq.ChatGroq, llm.ChatGoogleGenerativeAI, embedder._embed_once, client.StdioServerParameters)
    with seams.installed():
        assert langchain_groq.ChatGroq is models.groq and embedder._embed_once is models.embed
    assert (
        langchain_groq.ChatGroq,
        llm.ChatGoogleGenerativeAI,
        embedder._embed_once,
        client.StdioServerParameters,
    ) == real


def test_the_tool_server_is_the_real_one_started_after_the_providers_are_stood_in_for(at_once):
    from loadtest.standins import tool_server

    order: list[str] = []
    real = tools.httpx
    try:
        with (
            patch(
                "loadtest.standins.tool_server.runpy.run_module",
                lambda name, run_name: order.append(f"{name} as {run_name}"),
            ),
            patch.object(tool_server.providers, "install", lambda: order.append("providers stood in for")),
            patch.object(tool_server.guard, "check", lambda **urls: order.append(f"checked {sorted(urls)}")),
        ):
            tool_server.main()
    finally:
        tools.httpx = real

    assert order == ["checked ['REDIS_URL']", "providers stood in for", "src.ai.mcp_server.server as __main__"]
    assert seams.TOOL_SERVER == tool_server.__name__


# ── The app cannot tell them from the real thing: the providers ────────────


def test_every_trip_the_load_test_asks_for_is_found_by_the_real_tools(stand_in_providers):
    """The k6 script's destinations and interests, searched by the tools as they are — no ToolError anywhere."""
    destinations, interests = k6_list("DESTINATIONS"), k6_list("INTERESTS")
    assert len(destinations) == 12 and len(interests) == 3

    for destination in destinations:
        assert tools._city_to_iata(destination), f"the flight tool knows no airport for {destination}"
        assert destination.lower() in providers.PLACES, f"the stand-ins have no {destination}"
        for wanted in interests:
            flights, hotels, attractions = searched(destination, wanted)
            # as much as the real searches return at their limits: enough places for every day of the trip
            assert len(flights) == 4 and len(hotels) == 5 and len(attractions) == 10
            # everything a stand-in makes says that it is one
            assert all(hotel["name"].startswith("Stand-in ") for hotel in hotels)
            assert all(place["name"].endswith("(stand-in)") for place in attractions)
            assert all(place["lat"] is not None and place["interests"] for place in attractions)
            # the flights leave more than half the smallest budget the script asks with: the plan goes ahead
            assert make_budget_decision(flights, 60_000, 0, None).decision == "continue", destination


def test_a_search_is_cached_under_the_keys_the_real_ones_are(stand_in_providers):
    searched("Jaipur", ["history", "food"])
    first = set(stand_in_providers)
    searched("Jaipur", ["history", "food"])

    assert {key.split(":")[1] for key in first} == {"flights", "hotels", "attractions", "geocode"}
    assert set(stand_in_providers) == first  # the second search asked no provider: nothing new was stored


def test_a_search_for_food_takes_a_second_request_as_the_real_one_does(stand_in_providers):
    """The real provider rates almost no restaurant as well known, so a food search fills up from a second request."""
    asked: list[tuple[str, int]] = []
    answered = providers._answer

    def counting(method, url, params, body):
        if "opentripmap" in url:
            asked.append((params["kinds"], int(params["rate"])))
        return answered(method, url, params, body)

    with patch.object(providers, "_answer", counting):
        tools.get_attractions(AttractionInput(destination="Jaipur", interests=["history", "food"], limit=10))

    assert asked == [
        ("historic,museums,cultural", 2),
        ("foods,restaurants,cafes", 2),
        ("foods,restaurants,cafes", 1),
    ]


def test_a_request_to_anywhere_else_is_not_sent(at_once):
    with pytest.raises(providers.NoSuchProvider, match="api.example.com is none"):
        providers.get("https://api.example.com/v1/anything", params={"key": "value"})
    assert issubclass(providers.NoSuchProvider, httpx.ConnectError)  # to the tools: a provider that cannot be reached


def test_only_the_requests_are_stood_in_for(stand_in_providers):
    assert tools.httpx.get is providers.get and tools.httpx.post is providers.post
    assert tools.httpx.HTTPStatusError is httpx.HTTPStatusError  # everything else the tools name is the library's
    assert httpx.get is not providers.get  # …and the library itself is untouched, for whoever else uses it


def test_a_provider_is_waited_for_on_the_thread_that_asked_it():
    """A real request holds its worker thread for as long as the provider takes: so does a stand-in."""
    slept: list[tuple[str, float]] = []
    with patch.object(
        providers.time, "sleep", lambda seconds: slept.append((threading.current_thread().name, seconds))
    ):
        worker = threading.Thread(
            target=lambda: providers.post("https://api.duffel.com/air/offer_requests", json={}), name="a-tool"
        )
        worker.start()
        worker.join()

    ((thread, seconds),) = slept
    assert thread == "a-tool"
    assert timing.PROVIDERS["duffel"].fastest <= seconds <= timing.PROVIDERS["duffel"].slowest


# ── …the models ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stand_ins_plan_passes_the_builders_checks_and_the_evaluators(stand_in_providers):
    """For every destination: the real searches, the real builder asking the stand-in, the real evaluator."""
    for destination in k6_list("DESTINATIONS"):
        flights, hotels, attractions = searched(destination, ["history", "food"])
        trip = {
            "destination": destination,
            "start_date": str(START),
            "end_date": str(START + timedelta(days=3)),
            "group_size": 2,
        }
        with seams.installed():
            draft = await builder.build_itinerary(trip, flights, hotels, attractions)

        assert isinstance(draft, builder.ItineraryDraft), f"{destination}: {draft}"
        plan = draft.model_dump()
        estimate = evaluator.itinerary_estimate(plan, flights, hotels, destination=destination, month=START.month)
        verdict = evaluator.evaluate_itinerary(
            plan,
            trip["start_date"],
            trip["end_date"],
            estimate.total,
            attractions,
            budget_range=(estimate.total_min, estimate.total_max),
            hotels=hotels,
        )
        assert verdict.passed, f"{destination}: {[failure.detail for failure in verdict.failures]}"
        assert len(plan["days"]) == 4 and plan["days"][-1]["hotel"] is None  # four days, three nights


@pytest.mark.asyncio
async def test_a_stand_in_answers_through_langchain_so_its_call_is_metered_and_counted(stand_in_providers):
    flights, hotels, attractions = searched("Goa", ["beach", "nature"])
    trip = {"destination": "Goa", "start_date": str(START), "end_date": str(START + timedelta(days=3)), "group_size": 2}
    asked = metrics.LLM_CALLS.labels("itinerary_builder", llm.GROQ_MODEL, "ok")
    before = asked._value.get()

    with seams.installed():
        async with timed_run("itinerary_builder") as timer:
            draft = await builder.build_itinerary(trip, flights, hotels, attractions)

    assert isinstance(draft, builder.ItineraryDraft)
    assert asked._value.get() == before + 1  # llm_calls_total{agent, model}: the model that was asked for
    # the tokens are on the agent's meter — a build's row is of the size a real one is
    assert list(timer.usage.by_model) == [llm.GROQ_MODEL] and 2_500 < timer.usage.token_count < 6_000
    assert timer.usage.cost_usd  # …and priced, as the real model's are


@pytest.mark.asyncio
async def test_every_model_the_planner_asks_is_answered_as_it_expects(at_once):
    # the suite refuses the local guide a model unless a test hands it one (tests/conftest.py): here it gets its own
    the_guides_own_call = _REAL_SEAMS["src.ai.agents.destination_intelligence._call_llm"]
    with (
        seams.installed(),
        patch.object(destination_intelligence.settings, "GROQ_API_KEY", "standin-not-a-key"),
        patch.object(destination_intelligence, "_call_llm", the_guides_own_call),
    ):
        tips, error = await destination_intelligence.gather_intelligence(
            {"destination": "Jaipur", "month": "November", "days": 4, "travellers": 2, "interests": ["history"]}
        )
        # a trip the extractor's own arithmetic would call luxury: "mid-range" can only be what the model said
        luxury = {"avg_hotel_stars": 5, "per_person_per_night_inr": 20_000}
        assert preference_extractor.infer_travel_style(*luxury.values()) == "luxury"
        preferences = await preference_extractor.extract_preferences(luxury)
        intent = llm.parse_json_object(await llm.ask("Extract structured trip details. User message: forts and food"))
        from src.ai.agents.refinement_classifier import classify_refinement

        change = await classify_refinement("a nicer hotel, please", [])

    assert error is None and tips.local_transport and len(tips.best_times) == 3 and "Jaipur" in tips.local_transport
    assert preferences["travel_style"] == "mid-range" and preferences["dietary_restrictions"] == []
    assert intent["destination"] is None and intent["interests"] == [
        "sightseeing"
    ]  # nothing a trip's own fields lose to
    assert change.refinement_type == "targeted_hotel"


@pytest.mark.asyncio
async def test_the_itinerary_arrives_in_pieces_at_the_pace_a_model_writes(stand_in_providers):
    """The model thinks, then writes: the pieces are what the app batches, publishes and streams to every page."""
    flights, hotels, attractions = searched("Jaipur", ["history", "food"])
    trip = {
        "destination": "Jaipur",
        "start_date": str(START),
        "end_date": str(START + timedelta(days=3)),
        "group_size": 2,
    }
    pieces: list[str] = []
    waits: list[float] = []

    async def kept(piece: str) -> None:
        pieces.append(piece)

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    timing.scale = 1.0
    with seams.installed(), patch.object(models.asyncio, "sleep", no_wait):
        async with timed_run("itinerary_builder") as timer:
            draft = await builder.build_itinerary(trip, flights, hotels, attractions, on_token=kept)

    written = "".join(pieces)
    assert isinstance(draft, builder.ItineraryDraft) and json.loads(written)["days"]
    # the tokens come with the last piece, as the provider sends them: a streamed reply is metered like any other
    assert 2_500 < timer.usage.token_count < 6_000
    # each place's coordinates are copied from the list in the prompt, as the prompt asks — the map's pins
    first = json.loads(written)["days"][0]["morning"]
    place = next(found for found in attractions if found["name"] == first["activity"])
    assert place["lat"] is not None and (first["lat"], first["lng"]) == (place["lat"], place["lng"])
    assert 2_000 < len(written) < 3_000  # a live plan for four days was 2,599 characters
    assert all(len(piece) <= timing.PIECE_CHARACTERS for piece in pieces)
    thinking, writing = waits[0], sum(waits[1:])
    assert writing == pytest.approx(len(written) / timing.WRITING_CHARACTERS_A_SECOND, rel=0.01)
    assert timing.ITINERARY.fastest <= thinking + writing <= timing.ITINERARY.slowest  # the whole reply, as measured


@pytest.mark.asyncio
async def test_a_model_takes_as_long_as_it_was_measured_to():
    waited: list[float] = []
    with seams.installed(), patch.object(models.asyncio, "sleep", AsyncMock(side_effect=waited.append)):
        await llm.ask("Extract structured trip details. User message: a week of beaches")
        await models.embed("history and food trip to Jaipur", "RETRIEVAL_DOCUMENT", timeout=30)

    short, embedding = waited
    assert timing.SHORT_PROMPT.fastest <= short <= timing.SHORT_PROMPT.slowest
    assert timing.EMBEDDING.fastest <= embedding <= timing.EMBEDDING.slowest


def test_a_wait_is_most_often_near_the_usual_and_now_and_then_near_either_end():
    """Not the fastest every time, nor the usual every time: a tail is what fills a pool."""
    wait = timing.Wait(1.0, 2.0, 6.0)
    drawn = sorted(wait.seconds() for _ in range(4_000))

    assert drawn[0] < 1.3 and drawn[-1] > 5.0
    assert sum(drawn) / len(drawn) == pytest.approx(3.0, abs=0.1)  # a triangle's mean: a third of the three
    near_the_usual = sum(1 for seconds in drawn if 1.5 <= seconds <= 2.5)
    far_out = sum(1 for seconds in drawn if 4.5 <= seconds <= 5.5)
    assert near_the_usual > 2 * far_out


def test_the_waits_are_the_ones_the_report_says_were_measured():
    """docs/load-test-report.md, section 2: the table a reader judges the stand-ins by is the one they run on."""
    report = (ROOT / "docs" / "load-test-report.md").read_text()
    written = {}
    for line in report.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        found = re.fullmatch(r"([\d.]+) · ([\d.]+) · ([\d.]+) s.*", cells[-1]) if len(cells) == 4 else None
        if found:
            written[cells[0]] = timing.Wait(*map(float, found.groups()))

    assert written == {
        "Intent parsing (Gemini)": timing.SHORT_PROMPT,
        "Local tips (Groq)": timing.LOCAL_TIPS,
        "The itinerary (Groq)": timing.ITINERARY,
        "Preferences (Groq)": timing.PREFERENCES,
        "An embedding (Gemini)": timing.EMBEDDING,
        "Flights (Duffel)": timing.PROVIDERS["duffel"],
        "Hotels (LiteAPI)": timing.PROVIDERS["liteapi"],
        "Geocoding (Nominatim)": timing.PROVIDERS["nominatim"],
        "Attractions (OpenTripMap)": timing.PROVIDERS["opentripmap"],
    }
    # …and the pace a model writes at: 2,599 characters arrived in 1.78 seconds
    assert "1,460 characters a second" in report
    assert timing.WRITING_CHARACTERS_A_SECOND == pytest.approx(2599 / 1.78, rel=0.01)


def test_a_stand_in_provider_prices_a_trip_for_everyone_and_for_every_night():
    """The providers' own arithmetic, which the tools then read: a fare a traveller, a rate for the whole stay."""
    out = [{"origin": "DEL", "destination": "JAI", "departure_date": "2026-11-11"}]
    back = [{"origin": "JAI", "destination": "DEL", "departure_date": "2026-11-14"}]

    def cheapest(travellers: int, slices: list[dict]) -> float:
        asked = {"data": {"slices": slices, "passengers": [{"type": "adult"}] * travellers}}
        return min(float(offer["total_amount"]) for offer in providers.flights(asked)["data"]["offers"])

    alone = cheapest(1, out)
    assert alone > 0 and cheapest(3, out) == 3 * alone and cheapest(1, out + back) == 2 * alone

    def stay(checkout: str, rooms: int = 1) -> float:
        asked = {"cityName": "Jaipur", "checkin": "2026-11-11", "checkout": checkout, "occupancies": [{}] * rooms}
        return providers.hotels(asked)["data"][0]["roomTypes"][0]["offerRetailRate"]["amount"]

    a_night = stay("2026-11-12")
    assert a_night > 0 and stay("2026-11-14") == 3 * a_night and stay("2026-11-14", rooms=2) == 6 * a_night

    everything = ",".join(providers.BY_KIND)
    asked = {"lat": 26.9, "lon": 75.8, "kinds": everything, "rate": 1}
    assert len(providers.places({**asked, "limit": 50})) > 4 == len(providers.places({**asked, "limit": 4}))


def test_every_wait_is_between_the_fastest_and_the_slowest_it_was_seen_to_take():
    waits = [timing.SHORT_PROMPT, timing.LOCAL_TIPS, timing.ITINERARY, timing.PREFERENCES, timing.EMBEDDING]
    for wait in (*waits, *timing.PROVIDERS.values()):
        assert 0 < wait.fastest <= wait.usual <= wait.slowest
        drawn = [wait.seconds() for _ in range(200)]
        assert min(drawn) >= wait.fastest and max(drawn) <= wait.slowest
    assert set(timing.PROVIDERS) == set(providers.HOSTS.values())  # every provider that is answered for is waited for
    # none of them is instant, and a plan made of them takes what a real one takes: 10 to 16 seconds
    usual = timing.SHORT_PROMPT.usual + timing.LOCAL_TIPS.usual + timing.ITINERARY.usual
    assert 9 <= usual <= 13 and timing.scale == 1.0


@pytest.mark.asyncio
async def test_two_trips_of_the_same_kind_are_embedded_alike(at_once):
    """So GET /similar has something to find, as it has with the real model."""

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b)) / (sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)

    jaipur = await models.embed("history and food trip to Jaipur. Places: Jaipur Fort, Jaipur Palace", "DOC", timeout=1)
    again = await models.embed(
        "history and food trip to Jaipur. Places: Jaipur Fort, Jaipur Stepwell", "DOC", timeout=1
    )
    goa = await models.embed("beach and nature trip to Goa. Places: Goa North Beach, Goa Lakefront", "DOC", timeout=1)

    assert len(jaipur) == models.EMBEDDING_DIMENSIONS == 1536
    assert cosine(jaipur, again) > 0.84 > cosine(jaipur, goa)  # app/search.py's floor for "the same kind of trip"
    assert any(await models.embed("…", "QUERY", timeout=1))  # no words at all is still a direction


def test_asked_at_once_a_stand_in_still_answers(at_once):
    began = time.perf_counter()
    answer = providers.get("https://nominatim.openstreetmap.org/search", params={"q": "Jaipur"})
    asyncio.run(models.embed("a text", "DOC", timeout=1))

    assert answer.status_code == 200 and answer.json()[0]["address"]["country_code"] == "in"
    assert time.perf_counter() - began < 0.5
