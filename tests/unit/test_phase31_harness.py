"""Phase 31 — what the chaos exercises are made with: the server that fails, the faults, the script.

None of it is the app. All of it decides what the chaos report says: a fault that does not look
to a provider's client like the real failure tests nothing, and an exercise that calls a broken
thing fine is worse than none. So the failing server is spoken to by the providers' *own* clients
here — Google's and Groq's, made as the app makes them — and the script's arithmetic is held to
the page it imitates.
"""

import asyncio
import json
import re
import socket
import sys
import threading
import time
from importlib import util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn
import yaml

from loadtest.standins import fault_server, faults, guard, models
from src.ai import llm
from src.ai.builder import builder
from src.ai.usage import metered

ROOT = Path(__file__).resolve().parents[2]


def script(name: str):
    spec = util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


chaos = script("chaos")

# ── The server that fails ──────────────────────────────────────────────────

GEMINI = "/v1beta/models/gemini-3.5-flash:generateContent"
GROQ = "/openai/v1/chat/completions"


async def asked(
    path: str, method: str = "POST", leaves_after: float | None = None
) -> tuple[int | None, dict, dict | list | None]:
    """One request to the ASGI app itself: (status, headers, body) — (None, {}, None) if it was never answered."""
    sent: list[dict] = []
    gone = asyncio.Event()
    told = iter([{"type": "http.request", "body": b"{}", "more_body": False}])

    async def receive() -> dict:
        try:
            return next(told)
        except StopIteration:
            await gone.wait()
            return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    serving = asyncio.ensure_future(fault_server.app({"type": "http", "path": path, "method": method}, receive, send))
    if leaves_after is not None:
        await asyncio.sleep(leaves_after)
        assert not serving.done() and not sent  # still holding the request, having said nothing
        gone.set()
    await asyncio.wait_for(serving, 2)
    if not sent:
        return None, {}, None
    headers = {name.decode(): value.decode() for name, value in sent[0]["headers"]}
    return sent[0]["status"], headers, json.loads(sent[1]["body"])


@pytest.fixture
def forgotten():
    fault_server._seen.clear()
    yield
    fault_server._seen.clear()


async def test_a_429_is_worded_as_the_provider_words_it(forgotten):
    status, _, body = await asked("/429" + GEMINI)
    assert status == 429 and body["error"]["status"] == "RESOURCE_EXHAUSTED"
    (retry,) = [d for d in body["error"]["details"] if d["@type"].endswith("RetryInfo")]
    assert retry["retryDelay"] == "36s"  # what Gemini's free tier says: the wait the app must not sit out

    status, headers, body = await asked("/429" + GROQ)
    assert status == 429 and headers["retry-after"] == "2" and body["error"]["code"] == "rate_limit_exceeded"

    status, _, body = await asked("/500" + GROQ)
    assert status == 500 and "error" in body


async def test_a_request_that_hangs_is_read_and_never_answered(forgotten):
    status, _, _ = await asked("/hang" + GROQ, leaves_after=0.1)
    assert status is None  # nothing was ever sent back: only the client leaving ended it
    (kept,) = fault_server._seen
    assert kept["did"] == "hang" and kept["provider"] == "groq" and 0.1 <= kept["client_left_after_s"] < 1.0


async def test_it_keeps_what_it_was_asked_and_can_forget_it(forgotten):
    await asked("/429" + GEMINI)
    await asked("/429" + GROQ)
    _, _, log = await asked("/log", "GET")
    assert [(r["did"], r["provider"], r["method"]) for r in log] == [("429", "gemini", "POST"), ("429", "groq", "POST")]
    assert log[0]["at"] <= log[1]["at"] <= time.time() + 0.01  # kept to the millisecond
    await asked("/reset")
    assert (await asked("/log", "GET"))[2] == []


async def test_it_answers_nothing_correctly(forgotten):
    """A model that works is a stand-in. This server only fails — it cannot be mistaken for a provider."""
    status, _, body = await asked("/v1beta/models/gemini:generateContent")
    assert status == 404 and "only fails" in body["error"] and not fault_server._seen
    assert fault_server.FAULTS == faults.HOW == ("429", "500", "hang")


# ── Which model goes wrong ─────────────────────────────────────────────────


@pytest.fixture
def faults_file(tmp_path):
    file = tmp_path / "standin-faults.json"
    with patch.object(faults, "FILE", file):
        yield file


def test_no_file_is_no_fault_and_neither_is_one_that_cannot_be_read(faults_file):
    assert faults.on() == {}
    faults_file.write_text("{not json")
    assert faults.on() == {}
    faults_file.write_text('["gemini"]')
    assert faults.on() == {}
    faults_file.write_text(json.dumps({"gemini": "429", "builder": "hang", "postgres": "hang", "groq_small": "slowly"}))
    assert faults.on() == {"gemini": "429", "builder": "hang"}  # who and how are from two short lists
    assert str(faults.FILE.name) == Path(chaos.FAULTS_FILE).name  # the script writes the file the stand-ins read


def test_with_no_fault_on_every_model_is_a_stand_in(faults_file):
    assert isinstance(models.gemini(model="gemini-3.5-flash"), models.StandInChatModel)
    with metered("itinerary_builder"):
        assert isinstance(models.groq(model=llm.GROQ_MODEL, max_tokens=8192), models.StandInChatModel)


def test_a_fault_puts_the_providers_own_client_there_with_the_options_the_app_gave_it(faults_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "standin-not-a-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "standin-not-a-key")
    faults_file.write_text(json.dumps({"gemini": "429", "builder": "hang"}))

    from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
    from langchain_groq.chat_models import ChatGroq

    gemini = models.gemini(model="gemini-3.5-flash", temperature=0, max_retries=1, timeout=15)
    assert isinstance(gemini, ChatGoogleGenerativeAI) and gemini.max_retries == 1 and gemini.timeout == 15
    assert str(gemini.base_url) == f"{faults.FAR_END}/429"

    with metered("itinerary_builder"):
        builders = models.groq(model=llm.GROQ_MODEL, temperature=0, max_tokens=8192, timeout=20.0, max_retries=1)
    assert isinstance(builders, ChatGroq) and builders.request_timeout == 20.0 and builders.max_retries == 1
    assert str(builders.groq_api_base) == f"{faults.FAR_END}/hang"

    # the same large model, asked by somebody else: the fault is the builder's, not the model's
    with metered("destination_intelligence"):
        assert isinstance(models.groq(model=llm.GROQ_MODEL), models.StandInChatModel)
    assert isinstance(models.groq(model=llm.GROQ_SMALL_MODEL), models.StandInChatModel)  # no fault is on for it

    faults_file.write_text(json.dumps({"groq_small": "429"}))
    assert isinstance(models.groq(model=llm.GROQ_SMALL_MODEL, max_retries=1), ChatGroq)
    with metered("itinerary_builder"):
        assert isinstance(models.groq(model=llm.GROQ_MODEL), models.StandInChatModel)


def test_a_fault_never_points_a_client_at_anything_but_this_machine(faults_file):
    """The real client sends its key with every request. The stack's keys are placeholders — and still."""
    faults_file.write_text(json.dumps({"gemini": "429"}))
    with patch.object(faults, "FAR_END", "https://generativelanguage.googleapis.com"):
        with pytest.raises(guard.NotTheLoadTest, match="STANDIN_FAULT_SERVER"):
            models.gemini(model="gemini-3.5-flash")
    assert guard.is_this_machine(faults.FAR_END)


def test_nothing_in_the_app_knows_about_faults():
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        for name in ("loadtest.standins", "fault_server", "STANDIN_FAULT", "standin-faults"):
            assert name not in text, f"{path} mentions {name}"


# ── The providers' own clients, asked of the server that fails ─────────────


@pytest.fixture(scope="module")
def failing_server():
    """fault_server.py on a port of this machine, for the real clients to talk to."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fault_server.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


async def test_geminis_own_client_told_429_asks_once_and_the_other_model_answers(
    failing_server, forgotten, monkeypatch
):
    """Exercise 2, small: `llm.ask` as the app runs it, with Google's client getting Gemini's real quota error."""
    from langchain_google_genai.chat_models import ChatGoogleGenerativeAI

    monkeypatch.setenv("GOOGLE_API_KEY", "standin-not-a-key")

    def to_the_failing_server(**options):
        return ChatGoogleGenerativeAI(**{**options, "base_url": f"{failing_server}/429"})

    began = time.monotonic()
    with (
        patch.object(llm, "ChatGoogleGenerativeAI", to_the_failing_server),
        patch.object(llm, "_ask_groq", AsyncMock(return_value='{"interests": ["history"]}')) as instead,
        patch.object(llm.settings, "GROQ_API_KEY", "a-key"),
    ):
        answer = (
            await llm._REAL_ASK("Extract structured trip details.")
            if hasattr(llm, "_REAL_ASK")
            else await ask_for_real()
        )

    assert answer == '{"interests": ["history"]}' and instead.await_count == 1
    to_gemini = [r for r in fault_server._seen if r["provider"] == "gemini"]
    assert len(to_gemini) == 1  # max_retries=1 is one try: the 36 seconds it was told to wait are not waited
    assert time.monotonic() - began < 10


async def ask_for_real() -> str:
    """`llm.ask` itself — the suite hands most tests a stub of it (tests/conftest.py)."""
    from tests.fakes import _REAL_SEAMS

    return await _REAL_SEAMS.get("src.ai.llm.ask", llm.ask)("Extract structured trip details.")


async def test_groqs_own_client_given_no_answer_is_stopped_by_the_builders_deadline(
    failing_server, forgotten, monkeypatch
):
    """Exercise 3, small. With no timeout this client waited fifteen minutes and would have waited for ever."""
    from langchain_groq.chat_models import ChatGroq

    monkeypatch.setenv("GROQ_API_KEY", "standin-not-a-key")

    def to_the_failing_server(**options):
        return ChatGroq(**{**options, "base_url": f"{failing_server}/hang"})

    trip = {"destination": "Goa", "start_date": "2026-12-01", "end_date": "2026-12-03", "group_size": 2}
    began = time.monotonic()
    with (
        patch("langchain_groq.ChatGroq", to_the_failing_server),
        patch.object(builder, "BUILD_READ_TIMEOUT_S", 0.4),
        patch.object(builder, "BUILD_DEADLINE_S", 1.0),
    ):
        told = await builder.build_itinerary(
            trip, [], [{"name": "A hotel", "price_per_night_inr": 4000}], [], on_token=AsyncMock()
        )
    took = time.monotonic() - began

    assert isinstance(told, builder.BuilderError) and told.code == "LLM_TIMEOUT", told
    assert 0.9 <= took < 3.0  # the deadline, whatever the client was in the middle of
    hung = [r for r in fault_server._seen if r["did"] == "hang"]
    assert 1 <= len(hung) <= 3  # it asked, gave up on that try at its own timeout, and asked once more


# ── The script ─────────────────────────────────────────────────────────────


def test_the_page_the_script_imitates_reconnects_as_the_real_one_does():
    hook = (ROOT / "src/frontend/src/lib/sse.ts").read_text()
    backoff = re.search(r"const BACKOFF_MS = \[([^\]]+)\]", hook).group(1)
    assert tuple(int(ms.replace("_", "")) / 1000 for ms in backoff.split(",")) == chaos.PAGE_BACKOFF_S
    assert "?token=" in hook and '"token": self.token' in (ROOT / "scripts/chaos.py").read_text()


def test_the_roadmaps_numbers_are_the_scripts():
    assert chaos.BUILDER_MUST_FAIL_WITHIN_S == 60
    args = (ROOT / "scripts/chaos.py").read_text()
    assert 'add_argument("--away", type=int, default=30' in args  # "kill Redis for 30 seconds"
    assert chaos.PING_EVERY_S == 15  # sse-starlette's keep-alive: three missed is a stream nobody is serving


def test_every_integrity_question_counts_rows_that_must_not_exist():
    assert len(chaos.INTEGRITY) >= 6
    for what, query in chaos.INTEGRITY.items():
        assert query.lstrip().lower().startswith("select count(*)"), what
    asked_about = " ".join(chaos.INTEGRITY.values())
    for table in ("trips", "itineraries", "agent_runs", "embeddings"):
        assert table in asked_about
    assert "status = 'planning'" in asked_about  # a trip nobody is planning any more


def test_an_exercise_is_as_expected_only_when_everything_it_expected_was_so(tmp_path, capsys):
    seen = chaos.Seen()
    assert seen.expect(True, "the run ended") and not seen.expect(False, "the next plan works", "failed, 0 of 3")
    assert seen.failed == ["the next plan works"]
    with patch.object(chaos, "RECORDS", tmp_path), patch.object(chaos.loadtest, "commit", lambda: "abc"):
        code = chaos.keep("before", "mcp-kill-once", chaos.Clock(), seen, {"events": []})
    kept = json.loads((tmp_path / "before" / "mcp-kill-once.json").read_text())
    assert code == 1 and kept["as_expected"] is False and kept["exercise"] == "mcp-kill-once"
    assert [(r["ok"], r["what"]) for r in kept["expected"]] == [(True, "the run ended"), (False, "the next plan works")]
    assert "NOT AS EXPECTED (1)" in capsys.readouterr().out


def test_nothing_is_broken_on_a_stack_that_is_not_the_stand_ins(capsys):
    with patch.object(chaos.loadtest, "stand_ins_are_in", return_value=False):
        assert chaos.ready() is False
    assert "none of this is done to anything else" in capsys.readouterr().out
    main = (ROOT / "scripts/chaos.py").read_text()
    assert main.index("    if not ready():\n        return 1") < main.index(
        '    one = {\n        "mcp-kill"'
    )  # asked first


def test_the_tool_server_is_found_by_what_it_was_started_as():
    compile(chaos._FIND_TOOL_SERVER, "<find>", "exec")
    assert (
        "'tool_server' in cmd or 'mcp_server.server' in cmd" in chaos._FIND_TOOL_SERVER
    )  # the stand-in's, and the real one
    assert "os.kill(int(pid), 9)" in chaos._FIND_TOOL_SERVER  # SIGKILL: no chance to say goodbye


def test_the_failing_server_is_one_more_container_of_the_load_tests_own_image():
    overlay = yaml.safe_load((ROOT / "docker/chaos/compose.yml").read_text().replace("!reset []", "[]"))
    assert overlay["name"] == "tripplanner-prodlike"
    # the failing server, where the backend finds it, and (Phase 32) a Postgres that keeps its data when it is killed
    assert set(overlay["services"]) == {"provider-faults", "backend", "neon-standin"}
    far_end = overlay["services"]["provider-faults"]
    assert far_end["image"] == chaos.loadtest.LOADTEST_IMAGE and "build" not in far_end
    assert far_end["command"] == ["python", "-m", "loadtest.standins.fault_server"]
    assert not any("KEY" in name or "TOKEN" in name for name in far_end.get("environment", {}))
    backend = overlay["services"]["backend"]["environment"]
    assert backend == {"STANDIN_FAULT_SERVER": "http://provider-faults:9099"}  # where it is; nothing that turns it on
    assert guard.is_this_machine(backend["STANDIN_FAULT_SERVER"])
    assert chaos.FAULT_SERVER.endswith(":8209") and "8209:9099" in far_end["ports"]
    assert chaos.OVERLAY == ROOT / "docker/chaos/compose.yml"


def test_the_stack_can_be_given_one_more_file_to_lay_over_it():
    ran = MagicMock(return_value=MagicMock(returncode=0))
    with patch.object(chaos.loadtest, "run", ran):
        chaos.loadtest.compose("ps", also=(chaos.OVERLAY,))
    command = [str(part) for part in ran.call_args.args[0]]
    files = [command[i + 1] for i, part in enumerate(command) if part == "-f"]
    assert [Path(f).relative_to(ROOT).as_posix() for f in files] == [
        "docker/prodlike/compose.yml",
        "docker/loadtest/compose.yml",
        "docker/chaos/compose.yml",
    ]
    assert ".env.prodlike" in " ".join(command) and "tripplanner-ai/.env " not in " ".join(command) + " "
