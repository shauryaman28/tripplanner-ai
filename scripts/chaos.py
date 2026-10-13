#!/usr/bin/env python
"""Chaos (Phase 31): four things the app depends on, broken on purpose while it plans a trip.

    python scripts/chaos.py up                         the load test's stack, and a server for model calls that go wrong
    python scripts/chaos.py mcp-kill --when once       the tool server killed once, a second into a plan
    python scripts/chaos.py mcp-kill --when early      …kept dead from before any search has answered
    python scripts/chaos.py mcp-kill --when late       …kept dead from the moment the flights were found
    python scripts/chaos.py gemini-429 [--both]        Gemini answers 429 (--both: so does the model asked instead)
    python scripts/chaos.py builder-timeout            the model that writes the itinerary never answers
    python scripts/chaos.py redis-kill                 Redis stopped for 30 s under open event streams
    python scripts/chaos.py all                        each of them, one after another
    python scripts/chaos.py down

Nothing in the app is switched: a process is killed, a container is stopped, and a provider's own
client is pointed at a server that fails (loadtest/standins/faults.py). Each exercise then looks at
what a traveller would see — the event stream, GET /status, the rows in agent_runs — and at what
is left behind in Postgres, and keeps all of it: docs/chaos/<label>/<exercise>.json. An exercise
"passes" when the app does what docs/chaos-report.md says it should; the first run of each did not.

It only runs against the load test's stack on this machine: the same check that keeps a load off
real providers (scripts/loadtest.py) is made first.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8200"
FAULT_SERVER = "http://localhost:8209"
RECORDS = ROOT / "docs" / "chaos"
OVERLAY = ROOT / "docker" / "chaos" / "compose.yml"
BACKEND = "tripplanner-prodlike-backend-1"
REDIS = "tripplanner-prodlike-upstash-standin-1"
POSTGRES = "tripplanner-prodlike-neon-standin-1"
# The stack's own Redis, from this machine (docker/prodlike/compose.yml publishes it; its certificate is its own).
REDIS_URL = "rediss://default:standin_redis_password@localhost:6390?ssl_cert_reqs=none"
FAULTS_FILE = "/tmp/standin-faults.json"  # loadtest/standins/faults.py reads it

PAGE_BACKOFF_S = (1, 2, 4, 8, 16, 30)  # BACKOFF_MS in src/frontend/src/lib/sse.ts: how the page reconnects its stream
PING_EVERY_S = 15  # sse-starlette's keep-alive; a stream silent for much longer than this is not being served
ENDS = ("planning_complete", "planning_failed")
BUILDER_MUST_FAIL_WITHIN_S = 60  # the roadmap's number


def _script(name: str):
    """A file from scripts/ as a module, under a name of its own (loadtest.py shares its name with a package)."""
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"scripts_{name}"] = module
    spec.loader.exec_module(module)
    return module


loadtest = _script("loadtest")


def docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)


# ── What was seen ──────────────────────────────────────────────────────────


class Seen:
    """What an exercise expected and what it found, said as it goes and kept."""

    def __init__(self) -> None:
        self.results: list[dict] = []

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        print(f"  {'✓' if ok else '✗'} {what}{' — ' + detail if detail else ''}", flush=True)
        self.results.append({"ok": bool(ok), "what": what, "detail": detail})
        return bool(ok)

    def note(self, what: str) -> None:
        print(f"    {what}", flush=True)

    @property
    def failed(self) -> list[str]:
        return [result["what"] for result in self.results if not result["ok"]]


class Clock:
    """Seconds since an exercise began — on the clock that does not jump."""

    def __init__(self) -> None:
        self.began_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._zero = time.monotonic()
        self.awake = loadtest.Awake()  # whether this machine stays awake from here on (scripts/loadtest.py)

    def now(self) -> float:
        return round(time.monotonic() - self._zero, 2)


# ── A traveller, and the page they have open ───────────────────────────────


class Traveller:
    """Someone with an account on the stack, asking what the trip page asks."""

    def __init__(self) -> None:
        self.http = httpx.Client(base_url=API, timeout=60)
        account = {"email": f"chaos-{secrets.token_hex(5)}@example.com", "password": secrets.token_urlsafe(16)}
        self.http.post("/auth/register", json=account).raise_for_status()
        login = self.http.post("/auth/login", data={"username": account["email"], "password": account["password"]})
        self.token = login.raise_for_status().json()["access_token"]
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def trip(self, destination: str, ahead: int = 45) -> str:
        """A new trip. `ahead`: days from today — another day is another search nobody has cached."""
        start = date.today() + timedelta(days=ahead)
        asked = {
            "destination": destination,
            "start_date": str(start),
            "end_date": str(start + timedelta(days=3)),
            "budget": 70_000,
            "group_size": 2,
            "interests": ["history", "food"],
        }
        return self.http.post("/trips", headers=self.auth, json=asked).raise_for_status().json()["id"]

    def ask(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.http.request(method, path, headers=self.auth, **kwargs)

    def plan(self, trip: str) -> httpx.Response:
        return self.ask(
            "POST", f"/trips/{trip}/plan", json={"raw_input": "A relaxed long weekend of forts and good food"}
        )

    def status(self, trip: str) -> dict:
        """GET /status, or {"http": <code>} when it is not answered with one."""
        try:
            answer = self.ask("GET", f"/trips/{trip}/status", timeout=10)
        except httpx.HTTPError as failed:
            return {"http": type(failed).__name__}
        return answer.json() if answer.status_code == 200 else {"http": answer.status_code}

    def runs(self, trip: str) -> list[dict]:
        """The trip's agent_runs rows as the API gives them: who, how it ended, and why if it failed."""
        rows = self.ask("GET", f"/trips/{trip}/runs").json()
        return [
            {
                "agent": row["agent_name"],
                "status": row["status"],
                "ms": row["duration_ms"],
                "turn": row["turn"],
                "error": ((row.get("output") or {}).get("error") or (row.get("output") or {}).get("reason")),
            }
            for row in rows
        ]

    def itinerary(self, trip: str) -> dict | None:
        answer = self.ask("GET", f"/trips/{trip}/itinerary")
        return answer.json() if answer.status_code == 200 else None


class Page(threading.Thread):
    """A trip page's event stream, held open the way src/frontend/src/lib/sse.ts holds it.

    It connects with the token in the address; when the stream ends or fails it waits 1, 2, 4, 8,
    16, then 30 seconds and tries again; a `connected` event starts the count afresh. What it kept:
    every event with the second it arrived, and every connection with how it ended.
    """

    def __init__(self, traveller: Traveller, trip: str, clock: Clock, reconnects: bool = True) -> None:
        super().__init__(daemon=True)
        self.trip, self.token, self.clock, self.reconnects = trip, traveller.token, clock, reconnects
        self.events: list[dict] = []
        self.connections: list[dict] = []
        self.connected = threading.Event()
        self.ended = threading.Event()  # the run did: planning_complete or planning_failed arrived
        self._closing = threading.Event()
        self._answer: httpx.Response | None = None

    def is_connected(self) -> bool:
        return (
            bool(self.connections)
            and self.connections[-1].get("connected_at") is not None
            and ("ended_at" not in self.connections[-1])
        )

    def _listen_once(self) -> bool:
        """One connection, until it ends. True if it got as far as `connected`."""
        this = {"asked_at": self.clock.now(), "connected_at": None}
        self.connections.append(this)
        try:
            timeout = httpx.Timeout(10, read=PING_EVERY_S * 3)  # three keep-alives missed: nobody is serving it
            with httpx.stream(
                "GET", f"{API}/trips/{self.trip}/stream", params={"token": self.token}, timeout=timeout
            ) as answer:
                self._answer = answer
                this["http"] = answer.status_code
                kind = None
                for line in answer.iter_lines():
                    if self._closing.is_set():
                        break
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:") and kind == "connected":
                        this["connected_at"] = self.clock.now()
                        self.connected.set()
                    elif line.startswith("data:"):
                        try:
                            said = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        what = said.get("event") or f"{said.get('agent')}: {said.get('status')}"
                        if what != "builder_token":
                            kept = {"t": self.clock.now(), "event": what}
                            kept.update({k: said[k] for k in ("summary", "error", "retryable", "status") if k in said})
                            self.events.append(kept)
                        else:
                            self.events.append({"t": self.clock.now(), "event": what})
                        if said.get("event") in ENDS:
                            self.ended.set()
                this["how"] = "the stream ended"
        except httpx.ReadTimeout:
            this["how"] = f"open and silent for {PING_EVERY_S * 3} s — no event, no keep-alive"
        except httpx.HTTPError as failed:
            this["how"] = type(failed).__name__
        this["ended_at"] = self.clock.now()
        self.connected.clear()
        return this["connected_at"] is not None

    def run(self) -> None:
        attempt = 0
        while not self._closing.is_set():
            attempt = 0 if self._listen_once() else attempt
            if not self.reconnects or self._closing.is_set():
                return
            self._closing.wait(PAGE_BACKOFF_S[min(attempt, len(PAGE_BACKOFF_S) - 1)])
            attempt += 1

    def close(self) -> None:
        self._closing.set()
        if self._answer is not None:
            try:
                self._answer.close()
            except Exception:
                pass

    def named(self, *, pieces: bool = False) -> list[dict]:
        return [event for event in self.events if pieces or event["event"] != "builder_token"]

    def saw(self, event: str) -> dict | None:
        return next((kept for kept in self.events if kept["event"] == event), None)


def wait_for(condition, seconds: float, every: float = 0.25) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(every)
    return bool(condition())


def run_is_over(traveller: Traveller, trip: str, page: Page | None, seconds: float) -> dict:
    """Wait until the trip is no longer being planned — heard on the stream, or read from GET /status."""

    def over() -> bool:
        if page is not None and page.ended.is_set():
            return True
        return traveller.status(trip).get("status") in ("completed", "failed")

    wait_for(over, seconds, every=1.0)
    time.sleep(1.5)  # the rows written after the last event
    return traveller.status(trip)


# ── Breaking things ────────────────────────────────────────────────────────

_FIND_TOOL_SERVER = (
    "import os,sys\n"
    "found=[]\n"
    "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
    "    try: cmd=open(f'/proc/{pid}/cmdline','rb').read().replace(b'\\0',b' ').decode()\n"
    "    except OSError: continue\n"
    "    if 'tool_server' in cmd or 'mcp_server.server' in cmd:\n"
    "        if '-c' in cmd.split(): continue\n"
    "        found.append(int(pid))\n"
    "        if sys.argv[1:]==['kill']: os.kill(int(pid), 9)\n"
    "print(' '.join(map(str, found)))\n"
)


def tool_servers(kill: bool = False) -> list[int]:
    """The tool server's process ids inside the backend's container — and, asked to, SIGKILL for each."""
    answer = docker("exec", BACKEND, "python", "-c", _FIND_TOOL_SERVER, *(["kill"] if kill else []))
    return [int(pid) for pid in answer.stdout.split()]


def set_faults(**who_how: str) -> None:
    """Which model goes wrong, and how — or none: the file loadtest/standins/faults.py reads."""
    if who_how:
        docker("exec", "-i", BACKEND, "sh", "-c", f"cat > {FAULTS_FILE}", input=json.dumps(who_how))
    else:
        docker("exec", BACKEND, "rm", "-f", FAULTS_FILE)


def fault_log(reset: bool = False) -> list[dict]:
    """What the failing server was asked, and when — or forget it."""
    if reset:
        httpx.post(f"{FAULT_SERVER}/reset", timeout=10)
        return []
    return httpx.get(f"{FAULT_SERVER}/log", timeout=10).json()


def redis_clients() -> dict | None:
    """CLIENT LIST, as the roadmap asks: how many connections there are, and how many are subscribed. None: no Redis."""
    import redis

    try:
        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=3)
        listed = client.client_list()
        client.close()
    except redis.RedisError:
        return None
    others = [c for c in listed if c.get("cmd") != "client|list"]
    subscribed = [c for c in others if int(c.get("sub", 0)) > 0]
    return {
        "connections": len(others),
        "subscribed": len(subscribed),
        "channels": sum(int(c["sub"]) for c in subscribed),
    }


def streams_open() -> float | None:
    """How many event streams the backend says it is serving (GET /metrics)."""
    try:
        text = httpx.get(f"{API}/metrics", headers={"Authorization": f"Bearer {loadtest.ops_token()}"}, timeout=10).text
    except httpx.HTTPError:
        return None
    return loadtest.parse_metrics(text).get("streams_open", 0.0)  # no series yet: no stream has been opened


# This stack is told to send traces and error reports to receivers that are not running here; what the
# libraries say about that is not about the app.
_NOT_ABOUT_THE_APP = (
    "/v1/traces",
    "Retrying (Retry(",
    "Failed to export span batch",
    "IncompleteFieldDefinitionWarning",
    "[COST] no price",
)


def backend_log(since: str) -> list[str]:
    """The backend's log lines since a moment (RFC 3339), warnings and errors only — what an operator would be told."""
    said = docker("logs", "--since", since, BACKEND)
    kept = []
    for line in (said.stdout + said.stderr).splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        said = str(entry.get("message"))
        if any(noise in said for noise in _NOT_ABOUT_THE_APP):
            continue
        if entry.get("level") in ("warning", "error", "critical"):
            kept.append(f"{entry.get('level')}: {said[:300]}")
    return kept


# ── What is left behind ────────────────────────────────────────────────────

_SQL = ["docker", "exec", "-e", "PGPASSWORD=standin_db_password", POSTGRES]
_SQL += ["psql", "-X", "-A", "-t", "-U", "tripplanner", "-d", "tripplanner", "-c"]

# Each must count nothing. A trip is "planning" only while a run of it is alive; an itinerary belongs
# to a trip that completed; a run's rows end with the row that closes it; an itinerary's embeddings are
# two vectors, or one row that says they are still owed, or not there yet — never one of the two.
INTEGRITY = {
    "trips left in 'planning' with nothing planning them": "select count(*) from trips where status = 'planning'",
    "completed trips with no itinerary": (
        "select count(*) from trips t where status = 'completed' "
        "and not exists (select 1 from itineraries i where i.trip_id = t.id)"
    ),
    "failed trips whose only run saved an itinerary": (
        "select count(*) from trips t where status = 'failed' "
        "and exists (select 1 from itineraries i where i.trip_id = t.id) "
        "and (select max(turn) from agent_runs r where r.trip_id = t.id) = 1"
    ),
    "agent_runs rows that are neither completed nor failed": (
        "select count(*) from agent_runs where status not in ('completed', 'failed')"
    ),
    "trips whose rows do not end with the row that closes a run": (
        "select count(*) from trips t where status in ('completed', 'failed') and exists "
        "(select 1 from agent_runs r where r.trip_id = t.id) and "
        "(select agent_name from agent_runs r where r.trip_id = t.id order by created_at desc, "
        "(agent_name in ('orchestrator', 'interrupted')) desc limit 1) "
        "not in ('orchestrator', 'interrupted', 'preference_extractor', 'refinement_classifier')"
    ),
    "itineraries with half their embeddings": (
        "select count(*) from (select itinerary_id from embeddings where vector is not null "
        "group by itinerary_id having count(*) <> 2) half"
    ),
    "itineraries with both vectors and a row still owing them": (
        "select count(*) from (select itinerary_id from embeddings group by itinerary_id "
        "having count(*) filter (where vector is null) > 0 and count(*) filter (where vector is not null) > 0) mixed"
    ),
}


def sql(query: str) -> str:
    return subprocess.run([*_SQL, query], capture_output=True, text=True).stdout.strip()


def integrity(seen: Seen) -> dict:
    """Ask Postgres for each kind of row that must not exist. Waits a little first: the rows after a run's last event."""
    time.sleep(4)
    counted = {}
    for what, query in INTEGRITY.items():
        answer = sql(query)
        counted[what] = int(answer) if answer.isdigit() else answer
    wrong = {what: n for what, n in counted.items() if n != 0}
    seen.expect(not wrong, "nothing in Postgres is left half-done", json.dumps(wrong) if wrong else "")
    return counted


def keep(label: str, name: str, clock: Clock, seen: Seen, record: dict) -> int:
    out = RECORDS / label / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = {"exercise": name, "began": clock.began_at, "commit": loadtest.commit(), **record}
    kept["expected"] = seen.results
    kept["as_expected"] = not seen.failed
    # What happened stands whether or not the machine slept through part of it; how long it took does not.
    kept["asleep_s"] = round(clock.awake.asleep_for(), 1)
    kept["timings_hold"] = not clock.awake.slept()
    if clock.awake.slept():
        print(
            f"  this machine was asleep for {kept['asleep_s']:.0f} s of the exercise — is its lid closed, on battery? "
            "What happened stands; no time in this record means anything. Run it again awake.",
            flush=True,
        )
    out.write_text(json.dumps(kept, indent=1, default=str) + "\n")
    print(
        f"  {'AS EXPECTED' if not seen.failed else f'NOT AS EXPECTED ({len(seen.failed)})'}   → {loadtest.shown(out)}\n",
        flush=True,
    )
    return 0 if not seen.failed else 1


def a_plan(traveller: Traveller, destination: str, clock: Clock, ahead: int = 45, seconds: float = 120) -> dict:
    """One whole plan, nothing broken: is the stack able to make one? (Asked after every exercise.)"""
    trip = traveller.trip(destination, ahead=ahead)
    page = Page(traveller, trip, clock, reconnects=False)
    page.start()
    page.connected.wait(20)
    asked_at = clock.now()
    asked = traveller.plan(trip)
    status = run_is_over(traveller, trip, page, seconds) if asked.status_code == 202 else {"http": asked.status_code}
    page.close()
    progress = status.get("progress") or {}
    return {
        "trip": trip,
        "asked": asked.status_code,
        "status": status.get("status") or status.get("http"),
        "seconds": round(clock.now() - asked_at, 1),
        "searches_done": f"{progress.get('agents_done')} of {progress.get('agents_total')}",
        "errors": progress.get("errors") or {},
        "failure_reason": status.get("failure_reason"),
    }


# ── Exercise 1: the tool server is killed ──────────────────────────────────


KEEPS_DYING_S = 9  # for the two variants in which a new tool server is killed as soon as it starts


class KeptDead(threading.Thread):
    """Kill the tool server, and every one started in its place, for a while."""

    def __init__(self, seconds: float) -> None:
        super().__init__(daemon=True)
        self.seconds, self.killed = seconds, []

    def run(self) -> None:
        until = time.monotonic() + self.seconds
        while time.monotonic() < until:
            self.killed += tool_servers(kill=True)
            time.sleep(0.15)


def mcp_kill(label: str, when: str) -> int:
    """`when`: "once" — killed once, a second into the plan; "early" — it keeps dying from then on, so no
    search is answered; "late" — it keeps dying from the moment the flights have been found."""
    said = {
        "once": "killed once, in the middle of a plan",
        "early": "kept dead from before any search has answered",
        "late": "kept dead from the moment the flights were found",
    }[when]
    print(f"\n── Exercise 1 ({when}): the tool server is {said} ──", flush=True)
    clock, seen, traveller = Clock(), Seen(), Traveller()
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    destination, ahead = {"once": ("Kochi", 51), "early": ("Udaipur", 52), "late": ("Jodhpur", 53)}[when]
    if when == "late":
        # The same flights, cached by a first trip; a different budget and other interests, so that the
        # hotels and the attractions have to be searched for — and are still being, when the flights are in.
        first = a_plan(traveller, destination, clock, ahead=ahead)
        seen.note(f"a first trip to {destination}, planned whole: {first['status']}, searches {first['searches_done']}")
        start = date.today() + timedelta(days=ahead)
        trip = traveller.ask(
            "POST",
            "/trips",
            json={
                "destination": destination,
                "start_date": str(start),
                "end_date": str(start + timedelta(days=3)),
                "budget": 110_000,
                "group_size": 2,
                "interests": ["nature", "adventure"],
            },
        ).json()["id"]
    else:
        trip = traveller.trip(destination, ahead=ahead)  # nobody has searched this: nothing is cached
    before = tool_servers()
    page = Page(traveller, trip, clock)
    page.start()
    page.connected.wait(20)
    asked_at = clock.now()
    asked = traveller.plan(trip)
    if when == "late":
        wait_for(lambda: page.saw("flight_agent: completed") is not None, 40, every=0.02)
    else:
        time.sleep(1.0)  # the searches are under way (they began when the trip was made); none has answered
    killed_at = clock.now()
    if when == "once":
        killed = tool_servers(kill=True)
    else:
        keeping = KeptDead(KEEPS_DYING_S)
        keeping.start()
    print(f"  the tool server was killed {killed_at - asked_at:.1f} s after POST /plan", flush=True)

    status = run_is_over(traveller, trip, page, 120)
    ended_at = clock.now()
    if when != "once":
        keeping.join()
        killed = keeping.killed
    runs, plan = traveller.runs(trip), traveller.itinerary(trip)
    progress = status.get("progress") or {}
    searches = {r["agent"]: r for r in runs if r["agent"] in ("flight_agent", "hotel_agent", "activities_agent")}
    failed = {agent: row for agent, row in searches.items() if row["status"] == "failed"}
    codes = {
        agent: (row["error"] or {}).get("code") if isinstance(row["error"], dict) else None
        for agent, row in failed.items()
    }
    stream_failures = [event for event in page.named() if event.get("status") == "failed" and "summary" in event]
    words = [event.get("summary", "") for event in stream_failures] + list((progress.get("errors") or {}).values())
    itinerary = (plan or {}).get("structured_data") or {}

    seen.expect(asked.status_code == 202, "POST /plan was accepted", str(asked.status_code))
    seen.expect(
        status.get("status") in ("completed", "failed"),
        "the run ended — the trip is not left planning",
        f"{status.get('status')} {ended_at - killed_at:.1f} s after the kill",
    )
    if when == "once":
        seen.expect(
            status.get("status") == "completed" and progress.get("agents_done") == 3,
            "the plan is whole: a new tool server was started, and the searches were asked of it",
            f"{status.get('status')}, {progress.get('agents_done')} of {progress.get('agents_total')} searches, {json.dumps(progress.get('errors') or {})[:200]}",
        )
    else:
        seen.expect(bool(failed), "the searches it took are 'failed' rows in agent_runs", ", ".join(failed) or "none")
        seen.expect(
            bool(failed) and all(code == "CONNECTION_REFUSED" for code in codes.values()),
            "…each with a reason that names the cause",
            json.dumps({agent: row["error"] for agent, row in failed.items()})[:300],
        )
        seen.expect(
            bool(words) and not any("MCP" in said or "exception" in said.lower() for said in words),
            "the traveller is told in their own words, not the program's",
            "; ".join(dict.fromkeys(words))[:240],
        )
    if when == "early":
        seen.expect(status.get("status") == "failed", "with no search answered there is no plan: the trip is 'failed'")
        reason = status.get("failure_reason") or ""
        seen.expect(
            "No flights, places to stay" not in reason and bool(reason),
            "…and the reason given is that the searches could not be run, not that the destination has nothing",
            reason[:200],
        )
    if when == "late":
        seen.expect(
            status.get("status") == "completed" and plan is not None and 0 < (progress.get("agents_done") or 0) < 3,
            "the plan is made from what was found before it died",
            f"{status.get('status')}, {progress.get('agents_done')} of {progress.get('agents_total')} searches",
        )
        seen.expect(
            bool(itinerary.get("missing")),
            "…and the itinerary itself says what is missing from it, and why",
            json.dumps(itinerary.get("missing"))[:300]
            if itinerary.get("missing")
            else "nothing in the itinerary says so",
        )

    page.close()
    after = tool_servers()
    again = a_plan(traveller, {"once": "Madurai", "early": "Amritsar", "late": "Agra"}[when], clock, ahead=54)
    seen.expect(
        again["status"] == "completed" and not again["errors"],
        "the next plan works: a new tool server is running, and nobody restarted the backend",
        f"{again['status']}, searches {again['searches_done']}, {json.dumps(again['errors'])[:160]}",
    )
    counted = integrity(seen)
    return keep(
        label,
        f"mcp-kill-{when}",
        clock,
        seen,
        {
            "tool_server_pids": {
                "before": before,
                "killed": killed,
                "right_after_the_run": after,
                "now": tool_servers(),
            },
            "asked_at_s": asked_at,
            "killed_at_s": killed_at,
            "ended_at_s": ended_at,
            "events": page.named(),
            "status": status,
            "agent_runs": runs,
            "itinerary": {"days": len(itinerary.get("days") or []), "missing": itinerary.get("missing")}
            if plan
            else None,
            "the_next_plan": again,
            "integrity": counted,
            "log": backend_log(since)[:40],
        },
    )


# ── Exercise 2: Gemini answers 429 ─────────────────────────────────────────


def gemini_429(label: str, both: bool) -> int:
    who = "Gemini and the model asked instead" if both else "Gemini"
    print(f"\n── Exercise 2 ({'both' if both else 'gemini'}): {who} answer 429 ──", flush=True)
    clock, seen, traveller = Clock(), Seen(), Traveller()
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fault_log(reset=True)
    set_faults(gemini="429", **({"groq_small": "429"} if both else {}))
    try:
        trip = traveller.trip("Agra" if both else "Amritsar", ahead=56 if both else 55)
        page = Page(traveller, trip, clock)
        page.start()
        page.connected.wait(20)
        time.sleep(8)  # the traveller writes; the searches are warmed
        asked_at = clock.now()
        asked = traveller.plan(trip)
        status = run_is_over(traveller, trip, page, 180)
        took = round(clock.now() - asked_at, 1)
        asked_of = fault_log()
        runs = traveller.runs(trip)
        refined = None
        if both and status.get("status") == "completed":
            # a change request, with no model able to read it
            sent = traveller.ask("POST", f"/trips/{trip}/refine", json={"message": "a nicer hotel, please"}, timeout=90)
            page.ended.clear()
            after = run_is_over(traveller, trip, page, 180) if sent.status_code in (200, 202) else {}
            refined = {"http": sent.status_code, "answer": sent.text[:200], "status": after.get("status")}
    finally:
        set_faults()
        page.close()

    intent = next((r for r in runs if r["agent"] == "intent_parsing"), {})
    to_gemini = [r for r in asked_of if r["provider"] == "gemini"]
    to_groq = [r for r in asked_of if r["provider"] == "groq"]
    gaps = [round(b["at"] - a["at"], 2) for a, b in zip(asked_of, asked_of[1:])]
    seen.expect(len(to_gemini) >= 1, "Gemini's own client asked, and was told 429", f"{len(to_gemini)} request(s)")
    seen.expect(
        len(to_gemini) <= 2 and all(gap < 10 for gap in gaps),
        "it did not sit out the 36 seconds the error asks for",
        f"{len(to_gemini)} to Gemini, {len(to_groq)} to Groq; seconds between them: {gaps}",
    )
    seen.expect(
        status.get("status") == "completed", "the plan is made all the same", f"{status.get('status')} in {took} s"
    )
    seen.expect(took < 30, "…and in a plan's usual time", f"{took} s")
    if both:
        seen.expect(
            len(to_groq) >= 1, "the model asked instead was asked, and said 429 too", f"{len(to_groq)} request(s)"
        )
        seen.expect(intent.get("status") == "failed", "intent parsing is logged as failed", str(intent.get("status")))
        seen.expect(
            bool(refined) and refined["http"] in (200, 202) and refined["status"] == "completed",
            "a change request nobody could read is still carried out",
            json.dumps(refined)[:200],
        )
    else:
        seen.expect(
            intent.get("status") == "completed", "intent parsing was done by the other model", str(intent.get("status"))
        )
    counted = integrity(seen)
    return keep(
        label,
        "gemini-429-both" if both else "gemini-429",
        clock,
        seen,
        {
            "plan_seconds": took,
            "asked": asked.status_code,
            "requests_to_the_failing_server": asked_of,
            "seconds_between_them": gaps,
            "intent_parsing": intent,
            "status": status,
            "agent_runs": runs,
            "refinement": refined,
            "events": page.named(),
            "integrity": counted,
            "log": backend_log(since)[:40],
        },
    )


# ── Exercise 3: the builder's model never answers ──────────────────────────


def builder_timeout(label: str, give_up_after: int) -> int:
    print("\n── Exercise 3: the model that writes the itinerary never answers ──", flush=True)
    clock, seen, traveller = Clock(), Seen(), Traveller()
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fault_log(reset=True)
    set_faults(builder="hang")
    try:
        trip = traveller.trip("Varanasi", ahead=57)
        page = Page(traveller, trip, clock)
        page.start()
        page.connected.wait(20)
        time.sleep(8)
        asked_at = clock.now()
        asked = traveller.plan(trip)
        print(f"  asked for a plan; waiting for the trip to be marked failed (at most {give_up_after} s)…", flush=True)
        again_while_planning = None
        deadline = time.monotonic() + give_up_after
        status: dict = {}
        while time.monotonic() < deadline:
            status = traveller.status(trip)
            if status.get("status") in ("completed", "failed") or page.ended.is_set():
                break
            if again_while_planning is None and clock.now() - asked_at > 20:
                again_while_planning = traveller.plan(trip).status_code
            time.sleep(2)
        failed_after = round(clock.now() - asked_at, 1)
        time.sleep(2)
        status = traveller.status(trip)
        asked_of = fault_log()
    finally:
        set_faults()
    runs = traveller.runs(trip)
    builds = [r for r in runs if r["agent"] == "itinerary_builder"]
    stayed = [r.get("client_left_after_s") for r in asked_of]

    seen.expect(asked.status_code == 202, "POST /plan was accepted", str(asked.status_code))
    seen.expect(
        status.get("status") == "failed",
        "the trip is marked failed — not left planning",
        f"{status.get('status')} after {failed_after} s",
    )
    seen.expect(
        status.get("status") == "failed" and failed_after <= BUILDER_MUST_FAIL_WITHIN_S,
        f"…within {BUILDER_MUST_FAIL_WITHIN_S} seconds",
        f"{failed_after} s; the client asked {len(asked_of)} time(s) and stayed {stayed} s",
    )
    reason = status.get("failure_reason") or ""
    seen.expect(
        bool(reason)
        and "LLM call failed" not in reason
        and "Request timed out" not in reason
        and "within 30 s" not in reason,
        "the traveller is told why in their own words",
        reason[:200] or "no reason given",
    )
    seen.expect(
        page.saw("planning_failed") is not None,
        "the page is told on its stream",
        str(page.saw("planning_failed"))[:160],
    )
    seen.expect(
        again_while_planning == 409, "asking again while it was still trying was refused", str(again_while_planning)
    )
    page.close()

    # …and with the model answering again, the same trip can be planned
    page = Page(traveller, trip, clock, reconnects=False)
    page.start()
    page.connected.wait(20)
    retried_at = clock.now()
    retried = traveller.plan(trip)
    after = run_is_over(traveller, trip, page, 120) if retried.status_code == 202 else {}
    page.close()
    seen.expect(
        after.get("status") == "completed",
        "asked again once the model answers, the same trip is planned",
        f"{retried.status_code} → {after.get('status')} in {round(clock.now() - retried_at, 1)} s",
    )
    counted = integrity(seen)
    return keep(
        label,
        "builder-timeout",
        clock,
        seen,
        {
            "marked_failed_after_s": failed_after if status.get("status") == "failed" else None,
            "gave_up_waiting_after_s": give_up_after,
            "requests_to_the_failing_server": asked_of,
            "builds": builds,
            "status": status,
            "agent_runs": runs,
            "asked_again_while_planning": again_while_planning,
            "planned_again_afterwards": after.get("status"),
            "integrity": counted,
            "log": backend_log(since)[:40],
        },
    )


# ── Exercise 4: Redis is killed under open streams ─────────────────────────


def redis_kill(label: str, pages: int, away_s: int) -> int:
    print(f"\n── Exercise 4: Redis is killed for {away_s} s under {pages} open event streams ──", flush=True)
    clock, seen, traveller = Clock(), Seen(), Traveller()
    since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watched = traveller.trip("Leh", ahead=58)  # a trip that is only being looked at
    planned = traveller.trip("Srinagar", ahead=59)  # …and one that is being planned when Redis goes
    later = traveller.trip("Bhubaneswar", ahead=60)  # …and one whose plan is asked for while it is away
    time.sleep(9)  # their searches are warmed, and cached
    open_pages = [Page(traveller, watched, clock) for _ in range(pages)]
    planning_page = Page(traveller, planned, clock)
    for page in (*open_pages, planning_page):
        page.start()
    wait_for(lambda: all(page.connected.is_set() for page in (*open_pages, planning_page)), 30)
    time.sleep(1)
    before = {"redis": redis_clients(), "streams_open": streams_open()}
    seen.note(f"before: {before}")

    asked_at = clock.now()
    asked = traveller.plan(planned)
    time.sleep(3)  # intent parsing is done or nearly; the itinerary is not yet being written
    gone_at = clock.now()
    docker("kill", REDIS)
    print(f"  Redis killed {gone_at - asked_at:.1f} s after POST /plan", flush=True)

    during: list[dict] = []
    plan_while_away = None
    while clock.now() - gone_at < away_s:
        look = {
            "t": round(clock.now() - gone_at, 1),
            "pages_connected": sum(page.is_connected() for page in open_pages),
            "status_answers": traveller.status(planned).get("status") or traveller.status(planned).get("http"),
        }
        if plan_while_away is None and look["t"] >= 10:
            try:
                tried = traveller.ask("POST", f"/trips/{later}/plan", json={"raw_input": "temples"}, timeout=30)
                plan_while_away = {"http": tried.status_code, "answer": tried.text[:200]}
            except httpx.HTTPError as failed:
                plan_while_away = {"http": type(failed).__name__}
            plan_while_away["trip_status_right_after"] = sql(f"select status from trips where id = '{later}'")
        during.append(look)
        time.sleep(2)
    docker("start", REDIS)
    back_at = clock.now()
    print(f"  Redis started again after {back_at - gone_at:.1f} s", flush=True)

    everyone_back = wait_for(lambda: all(page.is_connected() for page in open_pages), 75, every=0.5)
    back_after = round(clock.now() - back_at, 1)
    reconnected_after = sorted(
        round(conn["connected_at"] - back_at, 1)
        for page in open_pages
        for conn in page.connections[-1:]
        if conn.get("connected_at") is not None and conn["connected_at"] >= back_at
    )
    status = run_is_over(traveller, planned, None, 90)
    time.sleep(2)
    after = {"redis": redis_clients(), "streams_open": streams_open()}
    seen.note(f"after: {after}")
    plan, runs = traveller.itinerary(planned), traveller.runs(planned)
    heard = [event["event"] for event in planning_page.named()]
    refine = traveller.ask("POST", f"/trips/{planned}/refine", json={"message": "a nicer hotel, please"}, timeout=90)
    if refine.status_code in (200, 202):
        run_is_over(traveller, planned, None, 120)

    ended = [page.connections[0].get("ended_at") for page in open_pages]
    ended_after = sorted(round(at - gone_at, 1) for at in ended if at is not None)
    seen.expect(
        len(ended_after) == pages or all(page.is_connected() for page in open_pages),
        "no stream is left open and silent: each ends, or goes on being served",
        f"{len(ended_after)} of {pages} ended, {ended_after[0] if ended_after else '—'} to {ended_after[-1] if ended_after else '—'} s after the kill",
    )
    seen.expect(
        everyone_back,
        "every page's stream is connected again after Redis returns — by the page's own backoff",
        f"{len(reconnected_after)} reconnected, {reconnected_after[0] if reconnected_after else '—'} to {reconnected_after[-1] if reconnected_after else '—'} s after it was back"
        if reconnected_after
        else f"all {pages} stayed connected"
        if everyone_back
        else f"{sum(page.is_connected() for page in open_pages)} of {pages} after {back_after} s",
    )
    expected = pages + 1
    seen.expect(
        after["redis"] is not None and after["redis"]["subscribed"] <= expected,
        "CLIENT LIST after the restart: no more subscribed connections than there are open streams",
        f"{(after['redis'] or {}).get('subscribed')} subscribed for {expected} streams (the backend counts {after['streams_open']})",
    )
    seen.expect(
        status.get("status") == "completed" and plan is not None,
        "the plan that was in flight is made, and saved",
        f"{status.get('status')}; the page heard: {', '.join(heard) or 'nothing'}",
    )
    answered = [look["status_answers"] for look in during]
    seen.expect(
        all(isinstance(answer, str) and answer in ("planning", "completed", "failed") for answer in answered),
        "GET /status goes on answering while Redis is away — it is what the page falls back on",
        f"{sorted(set(map(str, answered)))}",
    )
    seen.expect(
        plan_while_away is not None and plan_while_away["http"] in (202, 503),
        "a plan asked for while Redis is away is started, or refused in words — not a 500",
        json.dumps(plan_while_away)[:240],
    )
    seen.expect(
        plan_while_away is not None
        and (plan_while_away["http"] == 202 or plan_while_away.get("trip_status_right_after") != "planning"),
        "…and a refused one does not leave its trip 'planning'",
        str((plan_while_away or {}).get("trip_status_right_after")),
    )
    seen.expect(
        refine.status_code in (200, 202),
        "the planned trip can still be changed, although what Redis held about it is gone",
        f"{refine.status_code} {refine.text[:160]}",
    )

    for page in (*open_pages, planning_page):
        page.close()
    time.sleep(PING_EVERY_S + 6)  # the server notices a page has gone when it next writes to it
    gone = {"redis": redis_clients(), "streams_open": streams_open()}
    seen.note(f"after every page has closed: {gone}")
    seen.expect(
        gone["redis"] is not None and gone["redis"]["subscribed"] == 0,
        "with every page closed, CLIENT LIST shows no subscription left behind",
        f"{(gone['redis'] or {}).get('subscribed')} subscribed, {gone['streams_open']} streams open",
    )
    wait_for(lambda: sql(f"select status from trips where id = '{later}'") != "planning", 60, every=2)
    counted = integrity(seen)
    return keep(
        label,
        "redis-kill",
        clock,
        seen,
        {
            "pages": pages,
            "away_s": away_s,
            "asked": asked.status_code,
            "asked_at_s": asked_at,
            "redis_gone_at_s": gone_at,
            "redis_back_at_s": back_at,
            "before": before,
            "after_the_restart": after,
            "after_every_page_closed": gone,
            "streams_ended_s_after_the_kill": ended_after,
            "streams_reconnected_s_after_redis_was_back": reconnected_after,
            "attempts_of_one_page": open_pages[0].connections,
            "while_it_was_away": during,
            "a_plan_asked_for_while_it_was_away": plan_while_away,
            "the_plan_in_flight": {
                "status": status.get("status"),
                "saved": plan is not None,
                "heard_on_its_page": planning_page.named(),
                "agent_runs": runs,
            },
            "refine_afterwards": {"http": refine.status_code, "answer": refine.text[:200]},
            "integrity": counted,
            "log": list(dict.fromkeys(backend_log(since)))[:40],
        },
    )


# ── The stack ──────────────────────────────────────────────────────────────


def up() -> int:
    code = loadtest.up(also=(OVERLAY,))
    if code == 0:
        try:
            httpx.get(f"{FAULT_SERVER}/log", timeout=10).raise_for_status()
        except httpx.HTTPError as failed:
            print(
                f"the stack is up, but the server for failing model calls does not answer on {FAULT_SERVER}: {failed}"
            )
            return 1
        set_faults()
        print(f"…and {FAULT_SERVER} is where a model call goes when a fault is on.  Next: python scripts/chaos.py all")
    return code


def ready() -> bool:
    if not loadtest.stand_ins_are_in():
        print("the chaos stack is not up (python scripts/chaos.py up) — none of this is done to anything else.")
        return False
    return True


def fresh() -> bool:
    """Every exercise begins from a backend that has just started, with Redis up and no fault on.

    So that each is about one thing: what the last exercise left behind — a dead tool server, on the
    first runs — is not what the next one finds. (Whether the app gets over a fault *without* this is
    asked inside each exercise, before it ends.)
    """
    docker("start", REDIS)
    docker("restart", BACKEND)
    if not _script("prodlike").wait_until_ready(120):
        print("the backend did not come back after a restart.")
        return False
    set_faults()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="what", required=True)
    commands.add_parser("up", help="the load test's stack, and a server for model calls that go wrong")
    commands.add_parser("down", help="stop it and delete what it made")
    exercises = {
        "mcp-kill": "the tool server killed in the middle of a plan",
        "gemini-429": "Gemini answers 429",
        "builder-timeout": "the model that writes the itinerary never answers",
        "redis-kill": "Redis stopped under open event streams",
        "all": "each of them, one after another",
    }
    for name, said in exercises.items():
        exercise = commands.add_parser(name, help=said)
        exercise.add_argument("--label", default="run", help="what the records are kept under, in docs/chaos/")
        if name in ("mcp-kill",):
            exercise.add_argument("--when", choices=("once", "early", "late"), default="once")
        if name in ("gemini-429",):
            exercise.add_argument("--both", action="store_true", help="the model asked instead answers 429 too")
        if name in ("builder-timeout", "all"):
            exercise.add_argument("--give-up-after", type=int, default=900, help="seconds to wait for the trip to fail")
        if name in ("redis-kill", "all"):
            exercise.add_argument("--pages", type=int, default=20)
            exercise.add_argument("--away", type=int, default=30, help="seconds Redis stays down")
    args = parser.parse_args()

    if args.what == "up":
        return up()
    if args.what == "down":
        return loadtest.compose("down", "-v", "--remove-orphans", also=(OVERLAY,))
    if not ready():
        return 1
    one = {
        "mcp-kill": lambda: mcp_kill(args.label, args.when),
        "gemini-429": lambda: gemini_429(args.label, args.both),
        "builder-timeout": lambda: builder_timeout(args.label, args.give_up_after),
        "redis-kill": lambda: redis_kill(args.label, args.pages, args.away),
    }
    if args.what in one:
        return one[args.what]() if fresh() else 1
    every = (
        lambda: mcp_kill(args.label, "once"),
        lambda: mcp_kill(args.label, "early"),
        lambda: mcp_kill(args.label, "late"),
        lambda: gemini_429(args.label, both=False),
        lambda: gemini_429(args.label, both=True),
        lambda: builder_timeout(args.label, args.give_up_after),
        lambda: redis_kill(args.label, args.pages, args.away),
    )
    codes = [exercise() if fresh() else 1 for exercise in every]
    print(f"{codes.count(0)} of {len(codes)} exercises went as expected.")
    return max(codes)


if __name__ == "__main__":
    sys.exit(main())
