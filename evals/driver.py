"""Asks a running backend for each scenario's trip, over its API, as a traveller would (Phase 33).

Everything the grader is shown comes back through the API — the trip, its status, its itineraries, and
its agent runs (`GET /trips/{id}/runs`: every search's parameters and results, every step of the run).
Nothing is read from the database behind the backend's back: what is judged is what the build served.

A scenario's record — its *evidence* — is one JSON file, written whatever happened, so that it can be
graded again without asking the planner again (`run_eval.py --regrade`).

    serve()     this checkout's backend, started on a spare port against a database of its own
    Pacer       keeps plans far enough apart for the model provider's per-minute token quota
    run()       one scenario, start to end
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from evals.scenarios import Scenario

ROOT = Path(__file__).resolve().parents[1]

RUN_LIMIT_S = 330.0  # the app stops a run after 300 s (RUN_DEADLINE_S); a little longer, and it is the harness's fault
POLL_S = 1.5
EVAL_DB_SUFFIX = "_eval"
EVAL_REDIS_DB = 7

# Groq's free tier allows each model 8,000 tokens a minute, and one plan asks `gpt-oss-120b` for about
# 6,000 (the builder ~4,500, local tips and the preference extractor ~800 each). Two plans begun within
# the same minute leave the second builder refused (HTTP 429) and the plan failed — which would be
# scored as the build's fault. So a run that may build is begun no sooner than this after the last one
# that did. On a paid tier: --gap 0.
DEFAULT_GAP_S = 45.0

RUN_ENDS = ("orchestrator", "interrupted")  # every run ends with one of these rows (GET /status says so too)
REFUSED_WORDS = ("429", "rate limit", "rate_limit", "quota", "resource_exhausted", "too many requests")


class HarnessError(RuntimeError):
    """The eval could not do what it set out to — not something the planner did."""


class Awake:
    """How long this machine slept since this was made (as scripts/loadtest.py's: the wall clock goes on while
    a machine sleeps, the steady clock stops). A plan that was slept through can fail for that alone — a
    provider's connection gone stale under it — and its time says nothing about the build."""

    def __init__(self, wall=time.time, steady=time.monotonic) -> None:
        self._wall, self._steady = wall, steady
        self._wall0, self._steady0 = wall(), steady()

    def asleep_for(self) -> float:
        return max(0.0, (self._wall() - self._wall0) - (self._steady() - self._steady0))


# ── The backend ─────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def eval_database_url() -> str:
    """The dev database's address with `_eval` on its name: the eval's own, emptied before each run."""
    from app.core.config import settings
    from tests.database import use_database

    use_database(EVAL_DB_SUFFIX)
    return settings.DATABASE_URL


def eval_redis_url() -> str:
    """The dev Redis, database 7: the eval's own (the app and the tests use 0)."""
    from app.core.config import settings

    return urlunsplit(urlsplit(settings.REDIS_URL)._replace(path=f"/{EVAL_REDIS_DB}"))


def _fresh_database(database_url: str, redis_url: str) -> None:
    """Create and migrate the eval's database, empty it, and empty its Redis database."""
    import redis
    from sqlalchemy import create_engine, make_url, text

    from tests.database import create_and_migrate

    create_and_migrate()
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg2"))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users CASCADE"))  # every other table hangs off users
    engine.dispose()
    redis.Redis.from_url(redis_url).flushdb()


@contextmanager
def serve(log_path: Path) -> Iterator[str]:
    """This checkout's backend on a spare port, against the eval's own database and Redis; stopped after.

    Real models and real providers, from `.env` — it is the build that is being judged. Its log goes to
    `log_path`, beside the evidence.
    """
    database_url, redis_url = eval_database_url(), eval_redis_url()
    assert database_url.rsplit("/", 1)[-1].split("?")[0].endswith(EVAL_DB_SUFFIX), database_url
    _fresh_database(database_url, redis_url)

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "LOG_FORMAT": "text",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "src/backend", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_until_it_answers(url, backend, log_path)
            yield url
        finally:
            backend.terminate()
            try:
                backend.wait(30)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait(10)


def _wait_until_it_answers(url: str, backend: subprocess.Popen, log_path: Path, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if backend.poll() is not None:
            raise HarnessError(f"the backend stopped as it started — see {log_path}")
        try:
            if httpx.get(f"{url}/ping", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise HarnessError(f"the backend did not answer within {seconds:.0f} s — see {log_path}")


def answers(url: str) -> bool:
    try:
        return httpx.get(f"{url}/ping", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


# ── Pacing ──────────────────────────────────────────────────────────────────


class Pacer:
    """Begins a run that may call the itinerary builder no sooner than `gap_s` after the last one that did."""

    def __init__(self, gap_s: float, sleep=time.sleep, clock=time.monotonic) -> None:
        self.gap_s, self._sleep, self._clock = gap_s, sleep, clock
        self.last_build: float | None = None
        self.waited_s = 0.0

    def before_a_run(self) -> float:
        if self.last_build is None or self.gap_s <= 0:
            return 0.0
        wait = self.gap_s - (self._clock() - self.last_build)
        if wait > 0:
            self._sleep(wait)
            self.waited_s += wait
            return wait
        return 0.0

    def ran(self, began: float, built: bool) -> None:
        if built:
            self.last_build = began


# ── Talking to it ───────────────────────────────────────────────────────────


def _short(body: Any, limit: int = 600) -> Any:
    """What a leg of the record keeps of an answer: small ones whole, big ones (runs, plans) not at all."""
    text = str(body)
    return body if len(text) <= limit else f"{text[:limit]}…"


class Api:
    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(base_url=self.url, timeout=60)
        self.legs: list[dict] = []
        self.headers: dict[str, str] = {}

    def call(self, method: str, path: str, *, keep: bool = True, **kwargs) -> httpx.Response:
        began = time.perf_counter()
        response = self.client.request(method, path, headers=self.headers, **kwargs)
        if keep:
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            self.legs.append(
                {
                    "request": f"{method} {path}",
                    "sent": kwargs.get("json"),
                    "status": response.status_code,
                    "seconds": round(time.perf_counter() - began, 2),
                    "answer": _short(body),
                }
            )
        return response

    def sign_up(self) -> str:
        email = f"eval-{secrets.token_hex(5)}@example.com"
        password = secrets.token_urlsafe(16)
        made = self.call("POST", "/auth/register", json={"email": email, "password": password}, keep=False)
        if made.status_code != 201:
            raise HarnessError(f"could not make an account: {made.status_code} {made.text[:200]}")
        login = self.call("POST", "/auth/login", data={"username": email, "password": password}, keep=False)
        if login.status_code != 200:
            raise HarnessError(f"could not sign in: {login.status_code} {login.text[:200]}")
        self.headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        return email

    def get(self, path: str) -> Any:
        response = self.call("GET", path, keep=False)
        if response.status_code != 200:
            raise HarnessError(f"GET {path}: {response.status_code} {response.text[:200]}")
        return response.json()

    def runs(self, trip_id: str) -> list[dict]:
        return self.get(f"/trips/{trip_id}/runs")

    def ends(self, trip_id: str) -> int:
        return sum(1 for row in self.runs(trip_id) if row["agent_name"] in RUN_ENDS)

    def wait_for_the_run(self, trip_id: str, ends_before: int, limit_s: float = RUN_LIMIT_S, sleep=time.sleep) -> dict:
        """Until the run begun after `ends_before` run-ends has ended; its trip's GET /status."""
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            status = self.get(f"/trips/{trip_id}/status")
            if status["status"] != "planning" and self.ends(trip_id) > ends_before:
                return status
            sleep(POLL_S)
        raise HarnessError(f"the run did not end within {limit_s:.0f} s")


# ── One scenario ────────────────────────────────────────────────────────────


def split_runs(rows: list[dict]) -> list[list[dict]]:
    """A trip's agent_runs rows, run by run: each run ends with an orchestrator (or interrupted) row."""
    runs, current = [], []
    for row in rows:
        current.append(row)
        if row["agent_name"] in RUN_ENDS:
            runs.append(current)
            current = []
    if current:
        runs.append(current)  # one that never ended
    return runs


def built(rows: list[dict]) -> bool:
    return any(row["agent_name"] == "itinerary_builder" for row in rows)


def provider_refused(rows: list[dict]) -> bool:
    """A run that failed because a model provider turned it away for quota — not the build's doing."""
    for row in rows:
        if row["status"] == "failed" or row["agent_name"] == "builder_failed":
            said = str(row.get("output") or "").lower()
            if any(word in said for word in REFUSED_WORDS):
                return True
    return False


def refusal(detail) -> str:
    """A 422's detail in words: FastAPI's list of errors becomes their messages ("start_date cannot be in the past")."""
    if isinstance(detail, list):
        said = [
            str(error.get("msg", error)).removeprefix("Value error, ") for error in detail if isinstance(error, dict)
        ]
        return "; ".join(said)[:400] or str(detail)[:400]
    return str(detail)[:400]


def _choose(offered: list[dict], wanted: tuple[str, ...]) -> str | None:
    names = [option.get("choice") for option in offered]
    return next((choice for choice in wanted if choice in names), None)


def run(api: Api, scenario: Scenario, pacer: Pacer, base: dict | None = None) -> dict:
    """Ask for one scenario's trip and keep everything that came back. Never raises: the record says what failed."""
    began, awake = time.monotonic(), Awake()
    evidence: dict = {
        "scenario": scenario.id,
        "asked": {},
        "events": [],
        "outcome": None,
        "question": None,
        "conflict": None,
        "choice_taken": None,
        "failure_reason": None,
        "refinement": None,
        "trip_id": None,
        "trip": None,
        "status": None,
        "itineraries": [],
        "runs": [],
        "graded_turn": 1,
        "built": False,
        "provider_refused": False,
        "error": None,
    }
    try:
        _run(api, scenario, pacer, base, evidence)
    except (HarnessError, httpx.HTTPError) as exc:
        evidence["outcome"], evidence["error"] = "error", f"{type(exc).__name__}: {exc}"
    finally:
        evidence["legs"] = api.legs
        evidence["seconds"] = round(time.monotonic() - began, 1)
        evidence["asleep_s"] = round(awake.asleep_for(), 1)
    return evidence


def _start(
    api: Api, pacer: Pacer, trip_id: str, method: str, path: str, body: dict | None
) -> tuple[httpx.Response, float, int]:
    ends_before = api.ends(trip_id)
    pacer.before_a_run()
    return api.call(method, path, json=body), time.monotonic(), ends_before


def _after_the_run(api: Api, pacer: Pacer, trip_id: str, began: float, ends_before: int, evidence: dict) -> dict:
    status = api.wait_for_the_run(trip_id, ends_before)
    rows = split_runs(api.runs(trip_id))[-1]
    did_build = built(rows)
    pacer.ran(began, did_build)
    evidence["built"] = evidence["built"] or did_build
    evidence["provider_refused"] = evidence["provider_refused"] or provider_refused(rows)
    return status


def _run(api: Api, scenario: Scenario, pacer: Pacer, base: dict | None, evidence: dict) -> None:
    if scenario.refines:
        if not base or base.get("outcome") != "plan":
            evidence["outcome"] = "not run"
            evidence["error"] = f"the plan it changes ({scenario.refines}) was not made"
            return
        api.headers = dict(base["_headers"])
        trip_id = base["trip_id"]
        evidence["trip_id"], evidence["asked"] = trip_id, {"change": scenario.change, "of": scenario.refines}
        response, began, ends_before = _start(
            api, pacer, trip_id, "POST", f"/trips/{trip_id}/refine", {"message": scenario.change}
        )
        if response.status_code != 200:
            raise HarnessError(f"POST /refine: {response.status_code} {response.text[:200]}")
        evidence["refinement"] = response.json()
        evidence["graded_turn"] = response.json().get("turn", 2)
        evidence["events"].append("refined")
        status = _after_the_run(api, pacer, trip_id, began, ends_before, evidence)
        _ended(evidence, status)
        _collect(api, trip_id, evidence)
        return

    evidence["account"] = api.sign_up()
    trip = scenario.trip()
    evidence["asked"] = {
        "trip": trip,
        "message": scenario.message,
        "answer": scenario.answer,
        "choice": list(scenario.choice),
    }
    made = api.call("POST", "/trips", json=trip)
    if made.status_code == 422:
        evidence["outcome"], evidence["events"] = "refused", ["refused"]
        evidence["failure_reason"] = refusal(made.json().get("detail"))
        return
    if made.status_code != 201:
        raise HarnessError(f"POST /trips: {made.status_code} {made.text[:200]}")
    trip_id = made.json()["id"]
    evidence["trip_id"], evidence["_headers"] = trip_id, dict(api.headers)  # "_": never written to a file
    evidence["events"].append("created")

    body = {"raw_input": scenario.message} if scenario.message else {}
    response, began, ends_before = _start(api, pacer, trip_id, "POST", f"/trips/{trip_id}/plan", body)
    if response.status_code == 200 and response.json().get("status") == "clarification_needed":
        evidence["question"] = response.json().get("question")
        evidence["events"].append("asked")
        if not scenario.answer:
            evidence["outcome"] = "question"
            _collect(api, trip_id, evidence)
            return
        response, began, ends_before = _start(
            api, pacer, trip_id, "POST", f"/trips/{trip_id}/clarify", {"answer": scenario.answer}
        )
        evidence["events"].append("answered")
    if response.status_code not in (200, 202):
        raise HarnessError(f"{api.legs[-1]['request']}: {response.status_code} {response.text[:200]}")
    status = _after_the_run(api, pacer, trip_id, began, ends_before, evidence)

    if status.get("budget_conflict"):
        evidence["conflict"] = status["budget_conflict"]
        evidence["events"].append("conflict")
        choice = _choose(status["budget_conflict"].get("options") or [], scenario.choice)
        if scenario.choice and choice is None:
            evidence["outcome"] = "conflict"
            evidence["error"] = "none of the ways out it would take was offered"
            _collect(api, trip_id, evidence)
            return
        if choice:
            evidence["choice_taken"] = choice
            response, began, ends_before = _start(
                api, pacer, trip_id, "POST", f"/trips/{trip_id}/replan", {"choice": choice}
            )
            if response.status_code != 200:
                raise HarnessError(f"POST /replan: {response.status_code} {response.text[:200]}")
            evidence["events"].append(f"chose {choice}")
            status = _after_the_run(api, pacer, trip_id, began, ends_before, evidence)
    _ended(evidence, status)
    _collect(api, trip_id, evidence)


def _ended(evidence: dict, status: dict) -> None:
    evidence["status"] = status
    if status["status"] == "completed":
        evidence["outcome"] = "plan"
    elif status.get("budget_conflict"):
        evidence["outcome"] = "conflict"
    else:
        evidence["outcome"] = "failed"
        evidence["failure_reason"] = status.get("failure_reason")
    ended = evidence["outcome"] if evidence["outcome"] != "plan" else "planned"
    if evidence["events"][-1:] != [ended]:  # a conflict with no way out taken is already the last thing that happened
        evidence["events"].append(ended)


def _collect(api: Api, trip_id: str, evidence: dict) -> None:
    evidence["trip"] = api.get(f"/trips/{trip_id}")
    evidence["status"] = evidence["status"] or api.get(f"/trips/{trip_id}/status")
    evidence["itineraries"] = api.get(f"/trips/{trip_id}/itineraries")
    evidence["runs"] = api.runs(trip_id)
