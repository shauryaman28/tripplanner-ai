"""Phase 27 — errors reported to Sentry, and what is taken out of a report before it leaves.

The roadmap's acceptance — "one deliberate test error appears in Sentry" — is
held here on what the SDK would send: the app is switched on with a transport
that keeps every report instead of posting it, so each test reads a real
report, built by the real SDK from a real failing request, after the real
scrubber has been through it. (The same over HTTP, to a server that receives
it: tests/integration/test_phase27_production_integration.py, and the
production-like stack.)

The second half is the one that matters more. A report is made of whatever was
in reach when something failed, and here that is the login token in an event
stream's address, the providers' keys in the tools' request addresses, and
every secret the process holds. Each test below puts one of them in reach,
fails, and reads what would have left.
"""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import sentry_sdk
from httpx import ASGITransport, AsyncClient
from sentry_sdk.transport import Transport

from app import __version__
from app.core import error_reporting
from app.core.config import settings
from app.core.error_reporting import FILTERED, AccessLogFilter, Scrubber, secrets_of
from app.main import app
from tests.unit.test_phase27_production import GOOD, NEON, PROVIDERS, UPSTASH, deployment, providers

TOKEN = "t" * 64
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhLXVzZXIifQ.c2lnbmF0dXJlLW9mLXRoZS10b2tlbg"
EVERY_SECRET = [
    GOOD["JWT_SECRET"],
    GOOD["GOOGLE_API_KEY"],
    GOOD["GROQ_API_KEY"],
    GOOD["OPS_TOKEN"],
    "made-up-db-passw0rd",  # inside DATABASE_URL
    "AbCdUpstashPassw0rd",  # inside REDIS_URL
    PROVIDERS["DUFFEL_ACCESS_TOKEN"],
    PROVIDERS["LITEAPI_API_KEY"],
    PROVIDERS["OPENTRIPMAP_API_KEY"],
]


class Outbox(Transport):
    """Where the SDK puts its reports in these tests: a list, instead of a POST to a DSN."""

    sent: list = []

    def capture_envelope(self, envelope) -> None:
        Outbox.sent.append(envelope)

    def flush(self, timeout, callback=None) -> None:
        pass

    def kill(self) -> None:
        pass

    @classmethod
    def errors(cls) -> list[dict]:
        sentry_sdk.flush()
        return [envelope.get_event() for envelope in cls.sent if envelope.get_event()]

    @classmethod
    def traces(cls) -> list[dict]:
        sentry_sdk.flush()
        return [envelope.get_transaction_event() for envelope in cls.sent if envelope.get_transaction_event()]


@pytest.fixture
def reporting():
    """Switch error reporting on, as a deployment has it, into the Outbox. Off again afterwards."""
    Outbox.sent = []
    for scope in (sentry_sdk.get_global_scope(), sentry_sdk.get_isolation_scope(), sentry_sdk.get_current_scope()):
        scope.clear()  # breadcrumbs and tags are kept between reports: none of an earlier test's here

    def switch_on(**changes) -> type[Outbox]:
        assert error_reporting.configure(deployment(**changes), providers(), transport=Outbox) is True
        app.middleware_stack = None  # built again on the next request, with the SDK's hooks in it
        return Outbox

    yield switch_on
    error_reporting.switch_off()
    app.middleware_stack = None


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


def everything_in(report: dict) -> str:
    return json.dumps(report, default=str)


# ── On and off ─────────────────────────────────────────────────────────────


def test_reporting_is_off_without_a_dsn_and_in_the_test_stacks(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.error_reporting"):
        assert error_reporting.configure(deployment(SENTRY_DSN=""), providers(), transport=Outbox) is False
        assert not error_reporting.enabled()
        assert error_reporting.configure(deployment(APP_ENV="test"), providers(), transport=Outbox) is False
        assert not error_reporting.enabled()  # the test stacks raise on purpose all day

    assert "error reporting is off (SENTRY_DSN is empty)" in caplog.text
    assert "error reporting is off (APP_ENV is test)" in caplog.text
    error_reporting.shutdown()  # with reporting off there is nothing to send, and no error


def test_with_a_dsn_it_is_on_as_the_roadmap_says_and_carries_nothing_it_was_not_asked_to(reporting, caplog):
    with caplog.at_level(logging.INFO, logger="app.core.error_reporting"):
        reporting()

    options = sentry_sdk.get_client().options
    assert error_reporting.enabled()
    assert options["dsn"] == GOOD["SENTRY_DSN"] and options["traces_sample_rate"] == 0.1  # the roadmap's two
    assert (options["environment"], options["release"]) == ("production", f"tripplanner-ai@{__version__}")
    # what a report may carry
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert "error reporting is on — environment 'production'" in caplog.text
    assert not any(secret in caplog.text for secret in EVERY_SECRET)


async def test_the_app_switches_reporting_on_as_it_starts_and_sends_the_last_reports_as_it_stops():
    from app import main

    done: list = []
    with (
        patch.object(main.error_reporting, "configure", side_effect=lambda *given: done.append(("configure", given))),
        patch.object(main.error_reporting, "shutdown", side_effect=lambda: done.append("last reports sent")),
        patch.object(main.error_reporting, "quiet_access_log", side_effect=lambda: done.append("access log quiet")),
        patch.multiple(
            main,
            _show_app_logs=MagicMock(),
            init_redis=AsyncMock(return_value=AsyncMock()),
            _recover_after_restart=AsyncMock(),
            close_redis=AsyncMock(side_effect=RuntimeError("redis is gone")),
            close_session=AsyncMock(),
            finish_runs=AsyncMock(return_value=0),
        ),
    ):
        with pytest.raises(RuntimeError, match="redis is gone"):
            async with main.lifespan(main.app):
                # before a request is served: from the app's settings and the tool server's (their keys are scrubbed too)
                assert done == ["access log quiet", ("configure", (main.settings, main.mcp_settings))]
    assert done[-1] == "last reports sent"  # even when something else failed to close


# ── The roadmap's test: a deliberate error appears ─────────────────────────


async def test_the_deliberate_error_is_reported(reporting):
    outbox = reporting()
    marker = f"find-{uuid.uuid4().hex[:8]}"

    with patch.object(settings, "OPS_TOKEN", TOKEN):
        async with client() as http:
            failed = await http.get(f"/admin/sentry-test?marker={marker}", headers={"X-Ops-Token": TOKEN})

    assert failed.status_code == 500 and marker not in failed.text  # the traveller is told nothing of it…
    (report,) = outbox.errors()  # …and Sentry is told once
    error = report["exception"]["values"][-1]
    assert (error["type"], error["value"]) == ("DeliberateError", f"deliberate test error {marker}")
    assert error["mechanism"]["handled"] is False  # nobody caught it: that is what a 500 is
    assert (report["environment"], report["release"]) == ("production", f"tripplanner-ai@{__version__}")
    assert report["transaction"] == "/admin/sentry-test" and report["request"]["method"] == "GET"
    assert report["exception"]["values"][-1]["stacktrace"]["frames"][-1]["function"] == "sentry_test"


async def test_a_request_that_does_not_fail_reports_no_error_and_a_404_is_not_one(reporting):
    outbox = reporting()

    async with client() as http:
        assert (await http.get("/admin/sentry-test")).status_code == 404  # no token: the route is not there
        assert (await http.get("/no-such-route")).status_code == 404

    assert outbox.errors() == []


# ── What must not leave ────────────────────────────────────────────────────


async def test_the_login_token_in_an_address_and_the_operations_token_in_a_header_do_not_leave(reporting):
    """An event stream is opened as /trips/<id>/stream?token=<the login token>: a failing request's address is
    part of its report."""
    outbox = reporting(SENTRY_TRACES_SAMPLE_RATE=1.0)

    with patch.object(settings, "OPS_TOKEN", TOKEN):
        async with client() as http:
            await http.get(
                f"/admin/sentry-test?marker=m1&token={JWT}&api_key=a-provider-key-9",
                headers={"X-Ops-Token": TOKEN, "Authorization": f"Bearer {JWT}", "Cookie": "session=abc"},
            )

    (report,), (trace,) = outbox.errors(), outbox.traces()
    for sent in (report, trace):  # the error and the performance trace of the same request
        request = sent["request"]
        assert request["query_string"] == f"marker=m1&token={FILTERED}&api_key={FILTERED}"
        headers = {name.lower(): value for name, value in request["headers"].items()}
        assert headers["x-ops-token"] == FILTERED and headers["authorization"] == FILTERED
        assert headers["cookie"] == FILTERED
        assert "data" not in request  # no body, ever
        assert JWT not in everything_in(sent) and TOKEN not in everything_in(sent)
        assert "a-provider-key-9" not in everything_in(sent)


def test_no_secret_this_process_holds_leaves_wherever_it_turns_up(reporting):
    outbox = reporting()

    local_value = "-".join(["a", "local", "variable", "nobody", "should", "see"])

    def fails_with_everything_in_reach() -> None:
        password = local_value  # noqa: F841 — a local variable of the failing frame: the SDK's default sends them
        raise RuntimeError(
            f"could not connect to {NEON} or {UPSTASH}; keys {' '.join(EVERY_SECRET)}; "
            f"header Authorization: Bearer {JWT}; traveller asha.rao@example.com"
        )

    try:
        fails_with_everything_in_reach()
    except RuntimeError:
        sentry_sdk.capture_exception()
    logging.getLogger("app.api.routes.trips").error("Planning run crashed with %s in hand", GOOD["GROQ_API_KEY"])

    by_capture, by_log = outbox.errors()
    for report in (by_capture, by_log):
        sent = everything_in(report)
        assert not [secret for secret in EVERY_SECRET if secret in sent]
        assert JWT not in sent and "asha.rao@example.com" not in sent
        assert local_value not in sent
    assert all("vars" not in frame for frame in by_capture["exception"]["values"][-1]["stacktrace"]["frames"])
    said = by_capture["exception"]["values"][-1]["value"]
    # what is useful stays: which database, which Redis — without the way in
    assert f"postgresql://{FILTERED}@ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb" in said
    assert f"rediss://{FILTERED}@fine-owl-12345.upstash.io:6379" in said
    assert by_log["logentry"]["message"] == "Planning run crashed with %s in hand"
    assert by_log["logentry"]["params"] == [FILTERED]


async def test_a_providers_key_in_a_tools_request_address_does_not_leave(reporting):
    """The SDK notes every outgoing request down as a breadcrumb, address and all — and three of the providers
    take their key in the address."""
    outbox = reporting()
    key = PROVIDERS["OPENTRIPMAP_API_KEY"]

    answered = httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    async with httpx.AsyncClient(transport=answered) as provider:
        await provider.get(f"https://api.opentripmap.com/0.1/en/places/radius?radius=5000&apikey={key}&kinds=museums")
        await provider.get("https://api.openweathermap.org/data/2.5/forecast?appid=an-unlisted-key-77&q=Jaipur")
    sentry_sdk.add_breadcrumb(
        category="uvicorn.access",
        message=f'203.0.113.9:51544 - "GET /trips/1/stream?token={JWT} HTTP/1.1" 200',
    )
    sentry_sdk.capture_message("something else went wrong afterwards")

    (report,) = outbox.errors()
    crumbs = report["breadcrumbs"]["values"]
    sent = everything_in(report)
    assert key not in sent and "an-unlisted-key-77" not in sent and JWT not in sent  # by value, and by its name
    asked = [crumb for crumb in crumbs if crumb.get("category") == "httplib" and "//api." in crumb["data"]["url"]]
    assert [crumb["data"]["url"] for crumb in asked] == [
        "https://api.opentripmap.com/0.1/en/places/radius",
        "https://api.openweathermap.org/data/2.5/forecast",
    ]
    assert asked[0]["data"]["http.query"] == f"radius=5000&apikey={FILTERED}&kinds=museums"  # the rest is kept
    assert asked[1]["data"]["http.query"] == f"appid={FILTERED}&q=Jaipur"
    (access,) = [crumb for crumb in crumbs if crumb.get("category") == "uvicorn.access"]
    assert access["message"] == f'"GET /trips/1/stream?token={FILTERED} HTTP/1.1" 200'  # nor whose address it was


def test_a_run_is_reported_under_its_trip_and_leaves_nothing_on_anyone_elses_report(reporting):
    outbox = reporting()
    trip_id = uuid.uuid4()
    log = logging.getLogger("app.api.routes.trips")

    log.error("before any run")
    with error_reporting.run_scope(trip_id, turn=3):
        log.error("Planning run crashed for trip %s", trip_id)
    log.error("after the run")

    before, during, after = outbox.errors()
    assert during["tags"] == {"trip_id": str(trip_id), "turn": "3"}  # found in Sentry by its trip — as in LangSmith
    assert "trip_id" not in (before.get("tags") or {}) and "trip_id" not in (after.get("tags") or {})

    error_reporting.switch_off()
    with error_reporting.run_scope(trip_id, turn=1):  # with reporting off: nothing at all, and no error
        log.error("nobody is told")
    assert len(outbox.errors()) == 3


# ── The scrubber itself ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "scrubbed"),
    [
        (f"GET /trips/9/stream?token={JWT}&x=1", f"GET /trips/9/stream?token={FILTERED}&x=1"),
        ("https://api.x.com/v1?apikey=abc123&kinds=y", f"https://api.x.com/v1?apikey={FILTERED}&kinds=y"),
        ("…/forecast?q=Goa&appid=abc123", f"…/forecast?q=Goa&appid={FILTERED}"),
        ("grant_type=password&password=hunter2&username=a", f"grant_type=password&password={FILTERED}&username=a"),
        ("postgresql://app:s3cret@db.example.com/app", f"postgresql://{FILTERED}@db.example.com/app"),
        ("Authorization: Bearer abc.def-ghi_jkl", f"Authorization: Bearer {FILTERED}"),
        (f"the token {JWT} was refused", f"the token {FILTERED} was refused"),
        ("Key (email)=(asha.rao@example.co.in) already exists", f"Key (email)=({FILTERED}) already exists"),
        ("holds the-known-secret-value inside", f"holds {FILTERED} inside"),
        # …and what is not a secret is left alone
        ("tripplanner-ai@0.27.0", "tripplanner-ai@0.27.0"),  # a release, not an address
        ("token_count=4309 tokens=12", "token_count=4309 tokens=12"),
        ("No flights from DEL to JAI within ₹8,250.", "No flights from DEL to JAI within ₹8,250."),
        ("https://tile.openstreetmap.org/12/2925/1731.png", "https://tile.openstreetmap.org/12/2925/1731.png"),
    ],
)
def test_what_the_scrubber_takes_out_of_a_piece_of_text_and_what_it_leaves(text, scrubbed):
    assert Scrubber(["the-known-secret-value"]).text(text) == scrubbed


def test_a_value_is_also_judged_by_the_name_it_is_kept_under():
    cleaned = Scrubber([]).clean(
        {
            "headers": {"X-Ops-Token": "abc", "Authorization": "Basic dXNlcjpwYXNz", "Accept": "*/*"},
            "extra": {"access_token": "abc", "api-key": "abc", "client_secret": "abc", "password": "abc"},
            "usage": {"token_count": "4309", "tokens": "12", "turn": 2},
            "pairs": [["cookie", "session=abc"], ["accept", "*/*"]],
        }
    )

    assert cleaned["headers"] == {"X-Ops-Token": FILTERED, "Authorization": FILTERED, "Accept": "*/*"}
    assert set(cleaned["extra"].values()) == {FILTERED}
    assert cleaned["usage"] == {"token_count": "4309", "tokens": "12", "turn": 2}  # a count is not a token
    assert cleaned["pairs"] == [["cookie", FILTERED], ["accept", "*/*"]]


def test_the_secrets_looked_for_are_the_ones_these_settings_hold():
    found = secrets_of(deployment(DATABASE_URL="postgresql://app:p%40ss%2Fword-1@db.example.com/app"), providers())

    assert {GOOD["JWT_SECRET"], GOOD["GROQ_API_KEY"], GOOD["OPS_TOKEN"], PROVIDERS["DUFFEL_ACCESS_TOKEN"]} <= set(found)
    assert "AbCdUpstashPassw0rd" in found  # the password inside a URL
    assert {"p@ss/word-1", "p%40ss%2Fword-1"} <= set(found)  # as it is, and as it is written in the URL
    assert GOOD["SENTRY_DSN"] not in found  # a DSN only lets one send
    assert found == sorted(found, key=len, reverse=True)  # the longest first: one secret can contain another
    assert secrets_of(deployment(OPS_TOKEN="short")) == secrets_of(deployment(OPS_TOKEN=""))  # too short to look for


def test_a_report_that_cannot_be_scrubbed_is_not_sent(caplog):
    scrubber = Scrubber([])

    with patch.object(Scrubber, "clean", side_effect=RecursionError("too deep")), caplog.at_level(logging.WARNING):
        assert scrubber.event({"message": "x"}) is None  # dropped: better no report than an unscrubbed one
        assert scrubber.breadcrumb({"message": "x"}) is None
    assert "could not be scrubbed and was not sent" in caplog.text


# ── The server's own log ───────────────────────────────────────────────────


def test_the_access_log_does_not_keep_the_login_token():
    def logged(path: str) -> str:
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("203.0.113.9:1", "GET", path, "1.1", 200),
            None,
        )
        assert AccessLogFilter().filter(record) is True  # the line is kept — without the token
        return record.getMessage()

    assert (
        logged(f"/trips/9/stream?token={JWT}") == f'203.0.113.9:1 - "GET /trips/9/stream?token={FILTERED} HTTP/1.1" 200'
    )
    assert (
        logged("/trips/search?q=beaches+in+goa") == '203.0.113.9:1 - "GET /trips/search?q=beaches+in+goa HTTP/1.1" 200'
    )
    assert logged("/ping") == '203.0.113.9:1 - "GET /ping HTTP/1.1" 200'

    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    try:
        error_reporting.quiet_access_log()
        error_reporting.quiet_access_log()
        assert sum(isinstance(f, AccessLogFilter) for f in access.filters) == 1  # once, however often it is asked
    finally:
        access.filters[:] = before
