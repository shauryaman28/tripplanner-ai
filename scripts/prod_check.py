"""Is a deployment whole? The roadmap's Phase 27 acceptance, asked of the running thing from outside.

    python scripts/prod_check.py --prodlike                       # the stack scripts/prodlike.py started
    python scripts/prod_check.py --api https://<backend> --web https://<frontend> --env-file .env.production

What it asks, in order:

  1. the backend answers            GET /ready says Postgres and Redis are both there
  2. pgvector on the database       SELECT … FROM pg_extension WHERE extname = 'vector'; the schema is at
                                    the newest migration; the connection is encrypted
  3. Redis pub/sub over TCP         a message published on one connection arrives on another that subscribed
  4. an error reaches Sentry        GET /admin/sentry-test fails on purpose; the report is looked for — in the
                                    local stand-in, through Sentry's API, or by you (it says what to search for)
  5. the frontend answers           its page loads, and /api/* on its origin reaches the backend, uncached
  6. a trip is planned end to end   register → create → open the event stream as a browser does (straight to
                                    the backend, from the frontend's origin) → plan → the plan arrives.
                                    Every request that went through the frontend is timed.

Checks 2 and 3 need the database's and the Redis's URLs, check 4 the deployment's OPS_TOKEN: all read
from --env-file (the file the deployment's settings were put in), never from the command line. A check
that lacks what it needs is skipped and says so; the exit code is 0 only if nothing failed.

Check 6 plans a real trip: one request to each provider and to the models, and one account
(prod-check-…@example.com) left behind in the deployment's database. `--skip-plan` leaves it out.
No value from the env file is ever printed.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src" / "backend")]

PROXY_LIMIT_S = 120.0  # how long the frontend's host waits for the backend to begin to answer
PLAN_TIMEOUT_S = 180.0


def read_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None or not path.exists():
        return values
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


class Checks:
    def __init__(self) -> None:
        self.failed = self.passed = self.skipped = 0

    def ok(self, what: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  ✓ {what}" + (f" — {detail}" if detail else ""), flush=True)

    def fail(self, what: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  ✗ {what}" + (f" — {detail}" if detail else ""), flush=True)

    def skip(self, what: str, why: str) -> None:
        self.skipped += 1
        print(f"  – {what}: skipped ({why})", flush=True)

    def expect(self, condition: bool, what: str, detail: str = "") -> bool:
        (self.ok if condition else self.fail)(what, detail)
        return condition


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}{f':{parts.port}' if parts.port else ''}"


# ── 1 ──────────────────────────────────────────────────────────────────────


def backend_answers(checks: Checks, api: str) -> bool:
    print(f"\n1. The backend answers  ({api})")
    try:
        ready = httpx.get(f"{api}/ready", timeout=30)
    except Exception as exc:
        checks.fail("GET /ready", f"{type(exc).__name__}: {exc}")
        return False
    body = ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    checks.expect(ready.status_code == 200, "GET /ready is 200", f"{ready.status_code} {body}")
    checks.expect(
        ready.headers.get("cache-control") == "no-store",
        "answers say Cache-Control: no-store",
        f"cache-control: {ready.headers.get('cache-control')!r}",
    )
    try:
        version = httpx.get(f"{api}/openapi.json", timeout=30).json()["info"]["version"]
        from app import __version__

        checks.expect(
            version == __version__, "it is this version of the code", f"deployed {version}, here {__version__}"
        )
    except Exception as exc:
        checks.fail("its version could not be read", repr(exc))
    return ready.status_code == 200


# ── 2 ──────────────────────────────────────────────────────────────────────


def pgvector_is_there(checks: Checks, database_url: str | None) -> None:
    print("\n2. pgvector on the database")
    if not database_url:
        checks.skip("pgvector", "no DATABASE_URL in the env file")
        return
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    from app.db.url import sync_url, where

    print(f"   {where(database_url)}")
    try:
        engine = create_engine(sync_url(database_url))
        with engine.connect() as db:
            extension = db.execute(
                text("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
            ).first()
            if checks.expect(extension is not None, "SELECT … FROM pg_extension WHERE extname = 'vector' finds it"):
                version = tuple(int(part) for part in re.findall(r"\d+", extension.extversion)[:2])
                # search.py sets hnsw.iterative_scan, which pgvector has from 0.8 on
                checks.expect(version >= (0, 8), "pgvector is 0.8 or newer", f"version {extension.extversion}")
            at = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
            checks.expect(at == head, "the schema is at the newest migration", f"database {at}, code {head}")
            index = db.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'embeddings' AND indexdef ILIKE '%hnsw%'")
            ).first()
            checks.expect(index is not None, "the similarity index (HNSW) exists", index[0] if index else "missing")
            encrypted = db.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar()
            checks.expect(bool(encrypted), "the connection is encrypted")
        engine.dispose()
    except Exception as exc:
        checks.fail("the database could not be asked", f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")


# ── 3 ──────────────────────────────────────────────────────────────────────


def redis_pubsub_over_tcp(checks: Checks, redis_url: str | None) -> None:
    print("\n3. Redis pub/sub over TCP")
    if not redis_url:
        checks.skip("Redis", "no REDIS_URL in the env file")
        return
    scheme = redis_url.split("://", 1)[0]
    if scheme in ("http", "https"):
        checks.fail(
            "REDIS_URL is a REST address",
            "a REST API cannot subscribe — the event stream needs the rediss:// URL of the same database",
        )
        return
    import redis

    print(f"   {scheme}://{host_of(redis_url)}")
    try:
        client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=10, socket_timeout=10)
        checks.expect(client.ping() is True, "PING over TCP" + (" with TLS" if scheme == "rediss" else ""))
        channel, word = f"prod-check:{uuid.uuid4().hex}", uuid.uuid4().hex
        listener = client.pubsub()
        listener.subscribe(channel)
        deadline, heard = time.monotonic() + 10, None
        client.publish(channel, word)
        while time.monotonic() < deadline and heard is None:
            message = listener.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                heard = message["data"]
        listener.close()
        client.close()
        checks.expect(
            heard == word, "a published message arrives at a subscriber", "nothing arrived" if heard is None else ""
        )
    except Exception as exc:
        checks.fail("Redis could not be reached", f"{type(exc).__name__}: {str(exc)[:160]}")


# ── 4 ──────────────────────────────────────────────────────────────────────


def error_reaches_sentry(
    checks: Checks, api: str, env: dict[str, str], standin: str | None, secrets_: list[str]
) -> None:
    print("\n4. A deliberate error reaches Sentry")
    token = env.get("OPS_TOKEN") or env.get("PRODLIKE_OPS_TOKEN")
    if not token:
        checks.skip("GET /admin/sentry-test", "no OPS_TOKEN in the env file")
        return
    marker = f"check-{secrets.token_hex(6)}"
    closed = httpx.get(f"{api}/admin/sentry-test", params={"marker": marker}, timeout=30)
    checks.expect(
        closed.status_code == 404, "without the operations token the route is not there", str(closed.status_code)
    )
    failed = httpx.get(
        f"{api}/admin/sentry-test", params={"marker": marker}, headers={"X-Ops-Token": token}, timeout=30
    )
    told = (
        failed.json().get("error", {}) if failed.headers.get("content-type", "").startswith("application/json") else {}
    )
    checks.expect(
        failed.status_code == 500 and told.get("code") == "INTERNAL_SERVER_ERROR" and marker not in failed.text,
        "with it, the route fails on purpose — a 500 that says nothing of what went wrong",
        f"{failed.status_code} {failed.text[:80]!r}",
    )

    if standin:
        found: list = []
        for _ in range(20):
            found = [
                e
                for e in httpx.get(f"{standin}/events", params={"marker": marker}, timeout=10).json()
                if e["type"] == "event"
            ]
            if found:
                break
            time.sleep(0.5)
        if checks.expect(bool(found), "the report arrived at the stand-in", f"marker {marker}"):
            report = found[0]["payload"]
            error = report["exception"]["values"][-1]
            checks.ok("it is the error that was raised", f"{error['type']}: {error['value']}")
            print(f"     environment {report.get('environment')!r}, release {report.get('release')!r}")
        sent = "\n".join(httpx.get(f"{standin}/raw", timeout=10).json())
        leaked = [f"secret #{i}" for i, value in enumerate(secrets_) if value and value in sent]
        checks.expect(
            not leaked and "eyJ" not in sent,
            f"nothing secret in anything the stand-in was sent ({len(sent):,} characters, {len(secrets_)} values looked for)",
            ", ".join(leaked),
        )
        return

    auth, org, project = env.get("SENTRY_AUTH_TOKEN"), env.get("SENTRY_ORG"), env.get("SENTRY_PROJECT")
    if not (auth and org and project):
        print(f"     → look in Sentry for an issue containing: {marker}")
        print(
            "       (with SENTRY_AUTH_TOKEN, SENTRY_ORG and SENTRY_PROJECT in the env file this is read back for you)"
        )
        checks.skip("reading the report back from Sentry", "no SENTRY_AUTH_TOKEN / SENTRY_ORG / SENTRY_PROJECT")
        return
    region = "de.sentry.io" if ".de." in urlsplit(env.get("SENTRY_DSN", "")).netloc else "sentry.io"
    found_it = False
    for _ in range(30):  # Sentry takes a few seconds to show what it was sent
        try:
            listed = httpx.get(
                f"https://{region}/api/0/projects/{org}/{project}/events/",
                params={"full": "true"},
                headers={"Authorization": f"Bearer {auth}"},
                timeout=30,
            )
            if listed.status_code == 200 and marker in listed.text:
                found_it = True
                break
            if listed.status_code in (401, 403, 404):
                checks.fail("Sentry's API refused the question", f"HTTP {listed.status_code}")
                return
        except Exception:
            pass
        time.sleep(2)
    checks.expect(found_it, "the report is in Sentry (read back through its API)", f"marker {marker}")


# ── 5 ──────────────────────────────────────────────────────────────────────


def frontend_answers(checks: Checks, web: str | None) -> bool:
    print(f"\n5. The frontend answers  ({web or 'not given'})")
    if not web:
        checks.skip("the frontend", "no --web")
        return False
    try:
        page = httpx.get(f"{web}/login", timeout=60, follow_redirects=True)
        checks.expect(page.status_code == 200 and "<html" in page.text.lower(), "its page loads", str(page.status_code))
        began = time.perf_counter()
        through = httpx.get(f"{web}/api/ping", timeout=60)
        took = time.perf_counter() - began
        body = through.json() if "json" in through.headers.get("content-type", "") else {}
        reached = through.status_code == 200 and body.get("postgres") == "ok" and body.get("redis") == "ok"
        checks.expect(
            reached, "/api/* on its origin reaches the backend", f"{through.status_code} {body} in {took * 1000:.0f} ms"
        )
        cached = through.headers.get("x-vercel-cache")
        checks.expect(
            through.headers.get("cache-control") == "no-store" and cached not in ("HIT", "STALE"),
            "what comes back through it is not cached",
            f"cache-control {through.headers.get('cache-control')!r}"
            + (f", x-vercel-cache {cached}" if cached else ""),
        )
        return reached
    except Exception as exc:
        checks.fail("the frontend could not be reached", f"{type(exc).__name__}: {str(exc)[:160]}")
        return False


# ── 6 ──────────────────────────────────────────────────────────────────────


class Stream(threading.Thread):
    """The trip's event stream, read the way a browser's EventSource reads it."""

    def __init__(self, api: str, trip_id: str, token: str, origin: str | None) -> None:
        super().__init__(daemon=True)
        self.url = f"{api}/trips/{trip_id}/stream"
        self.params = {"token": token}
        self.headers = {"Accept": "text/event-stream", **({"Origin": origin} if origin else {})}
        self.events: list[dict] = []
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.error: str | None = None
        self.connected = threading.Event()
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            with httpx.stream(
                "GET",
                self.url,
                params=self.params,
                headers=self.headers,
                timeout=httpx.Timeout(30, read=PLAN_TIMEOUT_S),
            ) as response:
                self.status, self.response_headers = response.status_code, dict(response.headers)
                kind = None
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        try:
                            data = json.loads(line[5:].strip())
                        except ValueError:
                            data = {}
                        if kind == "connected":
                            self.connected.set()
                        else:
                            self.events.append(data)
                            if data.get("event") in ("planning_complete", "planning_failed", "budget_conflict"):
                                self.finished.set()
                                return
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self.connected.set()
            self.finished.set()


def a_trip_is_planned(checks: Checks, api: str, web: str | None, through_frontend: bool) -> None:
    base = f"{web}/api" if through_frontend else api
    print(f"\n6. A trip is planned end to end  (requests through {base}, the event stream straight to {api})")
    timings: list[tuple[str, float]] = []

    def call(method: str, path: str, **kwargs) -> httpx.Response:
        began = time.perf_counter()
        response = httpx.request(method, f"{base}{path}", timeout=PROXY_LIMIT_S + 10, **kwargs)
        timings.append((f"{method} {re.sub(r'[0-9a-f-]{36}', '<id>', path)}", time.perf_counter() - began))
        return response

    account = {"email": f"prod-check-{secrets.token_hex(5)}@example.com", "password": secrets.token_urlsafe(16)}
    if not checks.expect(call("POST", "/auth/register", json=account).status_code == 201, "an account is made"):
        return
    login = call("POST", "/auth/login", data={"username": account["email"], "password": account["password"]})
    if not checks.expect(login.status_code == 200, "it can sign in", str(login.status_code)):
        return
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    start = date.today() + timedelta(days=45)
    trip = call(
        "POST",
        "/trips",
        headers=auth,
        json={
            "destination": "Jaipur",
            "start_date": str(start),
            "end_date": str(start + timedelta(days=3)),
            "budget": 70_000,
            "group_size": 2,
            "interests": ["history", "food"],
        },
    )
    if not checks.expect(trip.status_code == 201, "a trip is created", str(trip.status_code)):
        return
    trip_id = trip.json()["id"]

    stream = Stream(api, trip_id, token, web)
    stream.start()
    stream.connected.wait(30)
    opened = stream.status == 200 and "text/event-stream" in stream.response_headers.get("content-type", "")
    checks.expect(opened, "the event stream opens, straight from the backend", stream.error or str(stream.status))
    if web:
        allowed = stream.response_headers.get("access-control-allow-origin")
        checks.expect(
            allowed == web,
            "the backend lets a page on the frontend's origin read it (CORS)",
            f"access-control-allow-origin: {allowed!r}",
        )

    began = time.perf_counter()
    plan = call(
        "POST",
        f"/trips/{trip_id}/plan",
        headers=auth,
        json={"raw_input": "A relaxed long weekend of forts and good food"},
    )
    answered = time.perf_counter() - began
    if not checks.expect(
        plan.status_code == 202, "POST /plan answers at once", f"{plan.status_code} in {answered * 1000:.0f} ms"
    ):
        print(f"     {plan.text[:200]}")
        return

    stream.finished.wait(PLAN_TIMEOUT_S)
    took = time.perf_counter() - began
    kinds = [event.get("event") or event.get("agent") for event in stream.events]
    pieces = sum(1 for kind in kinds if kind == "builder_token")
    last = stream.events[-1] if stream.events else {}
    checks.expect(
        last.get("event") == "planning_complete",
        "the plan arrives over the stream",
        f"{took:.1f} s; {len(stream.events)} events ({pieces} pieces of the itinerary as it was written); "
        f"ended with {last.get('event')!r}" + (f": {str(last.get('error'))[:120]}" if last.get("error") else ""),
    )

    for _ in range(40):  # the same, asked the other way: what a page that missed the stream would do
        status = call("GET", f"/trips/{trip_id}/status", headers=auth).json()
        if status.get("status") != "planning":
            break
        time.sleep(1.5)
    checks.expect(
        status.get("status") == "completed",
        "GET /status says completed",
        f"{status.get('status')}: {status.get('failure_reason') or ''}",
    )
    itinerary = call("GET", f"/trips/{trip_id}/itinerary", headers=auth)
    if checks.expect(itinerary.status_code == 200, "the itinerary is saved"):
        plan_data = itinerary.json()
        days = (plan_data.get("structured_data") or {}).get("days") or []
        print(f"     {len(days)} days, ₹{plan_data.get('total_cost'):,.0f}")
        pdf = call("GET", f"/trips/{trip_id}/export/pdf", headers=auth)
        checks.expect(
            pdf.status_code == 200 and pdf.content[:4] == b"%PDF", "it exports as a PDF", f"{len(pdf.content):,} bytes"
        )

    slowest = max(timings, key=lambda timing: timing[1])
    print("     every request that went through " + ("the frontend" if through_frontend else "the backend") + ":")
    for name, seconds in timings[:3] + sorted(timings[3:], key=lambda t: -t[1])[:4]:
        print(f"       {seconds * 1000:>7.0f} ms  {name}")
    checks.expect(
        slowest[1] < PROXY_LIMIT_S / 4,
        f"none of them comes near the {PROXY_LIMIT_S:.0f} s the frontend's host allows",
        f"the slowest, {slowest[0]}, took {slowest[1]:.1f} s",
    )
    print(f"     left behind: the account {account['email']} and its one trip")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask a deployment whether it is whole.")
    parser.add_argument("--api", help="the backend's address")
    parser.add_argument("--web", help="the frontend's address")
    parser.add_argument("--env-file", type=Path, help="the file the deployment's settings are in")
    parser.add_argument("--prodlike", action="store_true", help="check the stack scripts/prodlike.py started")
    parser.add_argument("--sentry-standin", help="a scripts/sentry_receiver.py to look for the report in")
    parser.add_argument("--skip-plan", action="store_true", help="do not plan a trip (check 6)")
    args = parser.parse_args()

    env = read_env(args.env_file)
    secrets_to_look_for: list[str] = []
    if args.prodlike:
        local = {**read_env(ROOT / ".env"), **read_env(ROOT / ".env.prodlike")}
        args.api = args.api or "http://localhost:8200"
        if args.web is None:
            try:
                httpx.get("http://localhost:3200/login", timeout=3)
                args.web = "http://localhost:3200"
            except Exception:
                print("(no frontend on http://localhost:3200 — its checks are skipped; see HOW_TO_RUN.md, Step 25)")
        args.sentry_standin = args.sentry_standin or "http://localhost:9010"
        env = {
            # the stack's own addresses, as this machine reaches them (docker/prodlike/compose.yml)
            "DATABASE_URL": "postgresql://tripplanner:standin_db_password@localhost:6543/tripplanner?sslmode=require",
            "REDIS_URL": "rediss://default:standin_redis_password@localhost:6390?ssl_cert_reqs=none",
            "OPS_TOKEN": local.get("PRODLIKE_OPS_TOKEN", ""),
            **env,
        }
        secrets_to_look_for = [
            value
            for name, value in local.items()
            if value and len(value) >= 8 and re.search(r"KEY|SECRET|TOKEN|PASSWORD", name)
        ] + ["standin_db_password", "standin_redis_password"]
    else:
        secrets_to_look_for = [
            v for n, v in env.items() if v and len(v) >= 8 and re.search(r"KEY|SECRET|TOKEN|PASSWORD", n)
        ]
    if not args.api:
        parser.error("give --api (and --web), or --prodlike")
    args.api, args.web = args.api.rstrip("/"), (args.web.rstrip("/") if args.web else None)

    checks = Checks()
    alive = backend_answers(checks, args.api)
    pgvector_is_there(checks, env.get("DATABASE_URL_UNPOOLED") or env.get("DATABASE_URL"))
    redis_pubsub_over_tcp(checks, env.get("REDIS_URL"))
    if alive:
        error_reaches_sentry(checks, args.api, env, args.sentry_standin, secrets_to_look_for)
    through_frontend = frontend_answers(checks, args.web)
    if args.skip_plan:
        print("\n6. A trip is planned end to end")
        checks.skip("planning a trip", "--skip-plan")
    elif alive:
        a_trip_is_planned(checks, args.api, args.web, through_frontend)

    print(f"\n{checks.passed} passed, {checks.failed} failed, {checks.skipped} skipped")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
