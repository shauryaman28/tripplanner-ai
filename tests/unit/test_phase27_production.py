"""Phase 27 — what a deployment asks of the app, and the files it is deployed with.

The roadmap's acceptance is about running services (a backend at a public
address, pgvector on a hosted Postgres, a test error in Sentry, a plan through
a hosted Redis and a hosted frontend). What can be held here, with nothing
running, is everything the first deploy got wrong:

- a hosted Postgres's URL, as it is handed out, connects — and a pooler can sit in between;
- the app says what a deployment is missing, all of it, before it starts, and refuses the
  repository's defaults in production;
- a hosted Redis's habits (closing connections) cost nothing; its password is in no log;
- a platform's health check gets a status it can read; nothing answered may be cached;
- the image listens where it is told, stops when it is told, and installs what was tested.

The running half is docker/prodlike + scripts/prod_check.py (HOW_TO_RUN.md, Step 25), and
tests/integration/test_phase27_production_integration.py on real Postgres and Redis.
"""

import json
import logging
import re
import shutil
import subprocess
from importlib import metadata
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app import preflight
from app.core.config import Settings, settings
from app.db import redis as redis_db
from app.db import session as db_session_module
from app.db.url import async_engine_arguments, normalised, sync_url, where
from app.main import NoStoreByDefault, app
from src.ai.mcp_server import cache as tool_cache
from src.ai.mcp_server.config import MCPSettings

ROOT = Path(__file__).resolve().parents[2]

# A hosted Postgres's connection string, as its dashboard shows it (the pooled one).
NEON = (
    "postgresql://neondb_owner:made-up-db-passw0rd@ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)
UPSTASH = "rediss://default:AbCdUpstashPassw0rd@fine-owl-12345.upstash.io:6379"


# ── The database URL ───────────────────────────────────────────────────────


def test_a_hosted_postgres_url_is_taken_as_it_is_handed_out():
    url, connect_args = async_engine_arguments(NEON)

    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.database, url.username) == (
        "ep-cool-name-pooler.ap-southeast-1.aws.neon.tech",
        "neondb",
        "neondb_owner",
    )
    assert url.password == "made-up-db-passw0rd"
    assert dict(url.query) == {}  # nothing left for SQLAlchemy to hand asyncpg as a keyword it does not have
    assert connect_args["ssl"] == "require"  # libpq's sslmode, under asyncpg's name
    assert not {"sslmode", "channel_binding"} & set(connect_args)


async def test_the_same_url_with_only_its_driver_swapped_cannot_connect():
    """What the app did before: postgresql:// → postgresql+asyncpg://, and nothing else. No network is needed
    to see it fail — asyncpg refuses the keyword before it looks for the host."""
    as_before = create_async_engine(NEON.replace("postgresql://", "postgresql+asyncpg://", 1))

    with pytest.raises(TypeError, match="sslmode"):
        async with as_before.connect():
            pass
    await as_before.dispose()


def test_nothing_prepared_is_kept_so_that_a_pooler_can_sit_in_between():
    _, connect_args = async_engine_arguments(NEON)

    assert connect_args["statement_cache_size"] == 0  # asyncpg's cache
    assert connect_args["prepared_statement_cache_size"] == 0  # SQLAlchemy's
    name = connect_args["prepared_statement_name_func"]
    assert name() != name() and re.fullmatch(r"__asyncpg_[0-9a-f]{32}__", name())  # never the same name twice
    # …and the app's own engine is made with them
    # (asked of the module when the engine is made — Phase 29 instruments it there, src/telemetry/traces.py)
    with patch("sqlalchemy.ext.asyncio.create_async_engine") as create:
        db_session_module.make_engine(NEON)
    (url,), made_with = create.call_args.args, create.call_args.kwargs
    assert url.drivername == "postgresql+asyncpg" and dict(url.query) == {}
    assert made_with["connect_args"]["statement_cache_size"] == 0 and made_with["connect_args"]["ssl"] == "require"
    # a serverless Postgres sleeps, and its connections die with it: tried before use, not kept past five minutes
    assert made_with["pool_pre_ping"] is True and made_with["pool_recycle"] == 300
    assert db_session_module.engine.pool._pre_ping is True and db_session_module.engine.pool._recycle == 300


def test_libpqs_other_parameters_are_translated_or_left_out_by_name(caplog):
    with caplog.at_level(logging.WARNING, logger="app.db.url"):
        url, connect_args = async_engine_arguments(
            "postgresql://u:p@db.example.com:6543/app?sslmode=verify-full&connect_timeout=7"
            "&options=endpoint%3Dep-cool-name&application_name=tripplanner&keepalives=1&target_session_attrs=read-write"
        )

    assert connect_args["ssl"] == "verify-full" and connect_args["timeout"] == 7.0
    assert connect_args["server_settings"] == {"options": "endpoint=ep-cool-name", "application_name": "tripplanner"}
    assert dict(url.query) == {} and url.port == 6543
    assert "keepalives, target_session_attrs left out" in caplog.text  # named, so nobody wonders — not passed on
    assert "read-write" not in caplog.text

    assert async_engine_arguments("postgresql://u:p@h/db?ssl=require")[1]["ssl"] == "require"  # asyncpg's own spelling
    assert "ssl" not in async_engine_arguments("postgresql://u:p@localhost/db")[1]  # a laptop's URL: untouched
    with pytest.raises(ValueError, match="sslmode must be one of"):
        async_engine_arguments("postgresql://u:p@h/db?sslmode=yes-please")


def test_both_drivers_get_the_url_they_can_read():
    assert normalised("postgres://u:p@h/db").drivername == "postgresql"  # as some platforms write it
    with pytest.raises(ValueError, match="must be a postgresql:// URL"):
        normalised("mysql://u:p@h/db")

    for_alembic = sync_url(NEON)
    assert for_alembic.startswith("postgresql+psycopg2://neondb_owner:made-up-db-passw0rd@ep-cool-name-pooler")
    # libpq's own parameters: nothing taken away
    assert set(for_alembic.split("?", 1)[1].split("&")) == {"sslmode=require", "channel_binding=require"}

    awkward = "postgresql://user:p%40ss%3Aw%2Frd@h/db?sslmode=require"  # a password with @ : / in it
    assert async_engine_arguments(awkward)[0].password == "p@ss:w/rd"
    assert "p%40ss%3Aw%2Frd@h" in sync_url(awkward)


def test_a_database_is_named_in_a_log_without_its_user_or_password():
    assert where(NEON) == "ep-cool-name-pooler.ap-southeast-1.aws.neon.tech/neondb"
    assert where("postgresql://u:secret@db.internal:6543/app") == "db.internal:6543/app"
    assert where("not a url at all") == "an unreadable URL"


# ── Preflight ──────────────────────────────────────────────────────────────

GOOD = {
    "APP_ENV": "production",
    "DATABASE_URL": NEON,
    "DATABASE_URL_UNPOOLED": "",
    "REDIS_URL": UPSTASH,
    "CORS_ORIGINS": "https://tripplanner.vercel.app",
    "CORS_ORIGIN_REGEX": "",
    "JWT_SECRET": "j" * 64,
    "GOOGLE_API_KEY": "google-key-value-0001",
    "GROQ_API_KEY": "groq-key-value-0002",
    "SENTRY_DSN": "https://publickey@o1.ingest.sentry.io/2",
    "OPS_TOKEN": "o" * 64,
    "LANGCHAIN_TRACING_V2": False,
    "LANGCHAIN_API_KEY": "",
    # Phase 29: a deployment that has everything has somewhere to send its traces
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
    "OTEL_EXPORTER_OTLP_HEADERS": "",
    "LOG_FORMAT": "",
}
PROVIDERS = {
    "DUFFEL_ACCESS_TOKEN": "duffel-token-value-0003",
    "LITEAPI_API_KEY": "liteapi-key-value-0004",
    "OPENTRIPMAP_API_KEY": "otm-key-value-0005",
    "OPENWEATHER_API_KEY": "",
    "REDIS_URL": UPSTASH,
}


def deployment(**changes) -> Settings:
    """Settings as a platform would give them — nothing read from this machine's .env."""
    return Settings(_env_file=None, **{**GOOD, **changes})


def providers(**changes) -> MCPSettings:
    return MCPSettings(_env_file=None, **{**PROVIDERS, **changes})


def test_a_deployment_that_has_everything_is_told_so():
    report = preflight.check(deployment(), providers())

    assert report.production and report.ok and report.errors == [] and report.warnings == []
    assert report.text() == "Every setting a deployment needs is there."
    assert preflight.enforce(deployment(), providers()) is not None


def test_the_repositorys_own_defaults_do_not_start_in_production_and_every_reason_is_said_at_once():
    shipped = {name: Settings.model_fields[name].default for name in GOOD}
    as_first_deployed = Settings(_env_file=None, **{**shipped, "APP_ENV": "production"})
    nothing_given = MCPSettings(_env_file=None, **{name: MCPSettings.model_fields[name].default for name in PROVIDERS})

    report = preflight.check(as_first_deployed, nothing_given)

    assert not report.ok
    said = report.text()
    for variable in ("JWT_SECRET", "DATABASE_URL", "REDIS_URL", "CORS_ORIGINS", "GROQ_API_KEY"):
        assert any(error.startswith(variable) for error in report.errors), variable  # cannot start
    for variable in ("GOOGLE_API_KEY", "DUFFEL_ACCESS_TOKEN", "LITEAPI_API_KEY", "OPENTRIPMAP_API_KEY", "SENTRY_DSN"):
        assert any(warning.startswith(variable) for warning in report.warnings), variable  # starts, worse
    assert said.startswith("Cannot start in production:") and "Starts, and will not fully work:" in said
    with pytest.raises(preflight.NotReady) as refused:
        preflight.enforce(as_first_deployed, nothing_given)
    assert str(refused.value) == said  # the one message has all of it

    # the same settings on a laptop stop nothing: a laptop is allowed its defaults
    on_a_laptop = Settings(_env_file=None, **{**shipped, "APP_ENV": "development"})
    assert preflight.check(on_a_laptop, nothing_given).ok
    assert preflight.enforce(on_a_laptop, nothing_given).text().startswith("Would not start in production:")


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"REDIS_URL": "https://fine-owl-12345.upstash.io"}, "REST API cannot subscribe"),
        ({"REDIS_URL": "redis://default:pw@fine-owl-12345.upstash.io:6379"}, "only answers over TLS: write rediss://"),
        ({"REDIS_URL": "redis://localhost:6379"}, "REDIS_URL points at this machine"),
        ({"REDIS_URL": "memcached://cache:11211"}, "REDIS_URL is not a redis:// or rediss:// URL"),
        ({"DATABASE_URL": "postgresql://u:p@localhost:5432/db"}, "DATABASE_URL points at this machine"),
        ({"DATABASE_URL": "mysql://u:p@db.example.com/db"}, "DATABASE_URL is not a postgresql:// URL"),
        ({"JWT_SECRET": "change-me-before-production"}, "JWT_SECRET is the default from the repository"),
        ({"JWT_SECRET": "short-secret"}, "JWT_SECRET is shorter than 32 characters"),
        ({"CORS_ORIGINS": "http://localhost:3000"}, "CORS_ORIGINS names no deployed frontend"),
        ({"CORS_ORIGINS": "https://tripplanner.vercel.app/"}, "is not an origin"),
        ({"CORS_ORIGINS": "tripplanner.vercel.app"}, "is not an origin"),
        ({"GROQ_API_KEY": ""}, "no plan could be made"),
    ],
)
def test_each_thing_that_stops_a_deployment_is_said_in_words(change, says):
    report = preflight.check(deployment(**change), providers())

    assert not report.ok and len(report.errors) >= 1
    assert says in report.text()


def test_what_only_degrades_a_deployment_does_not_stop_it():
    report = preflight.check(
        deployment(GOOGLE_API_KEY="", SENTRY_DSN="", LANGCHAIN_TRACING_V2=True),
        providers(DUFFEL_ACCESS_TOKEN="", LITEAPI_API_KEY=""),
    )

    assert report.ok and report.errors == []
    assert [warning.split(" ")[0] for warning in report.warnings] == [
        "GOOGLE_API_KEY",
        "DUFFEL_ACCESS_TOKEN",
        "LITEAPI_API_KEY",
        "SENTRY_DSN",
        "LANGCHAIN_TRACING_V2",
    ]
    # error reporting that is on but cannot be tried out, and a token too short to be one
    assert "OPS_TOKEN is empty" in preflight.check(deployment(OPS_TOKEN=""), providers()).text()
    assert "OPS_TOKEN is shorter" in preflight.check(deployment(OPS_TOKEN="hunter2"), providers()).text()
    # an origin list with only localhost is enough when a pattern lets the real one in
    behind_a_pattern = deployment(
        CORS_ORIGINS="http://localhost:3000", CORS_ORIGIN_REGEX=r"^https://app-.*\.vercel\.app$"
    )
    assert preflight.check(behind_a_pattern, providers()).ok


def test_a_variable_is_named_and_its_value_never_printed():
    wrong_everywhere = deployment(
        JWT_SECRET="tooshort-SECRETVALUE",
        DATABASE_URL="postgresql://dbuser:DBPASSWORDVALUE@localhost/db",
        REDIS_URL="https://default:REDISPASSWORDVALUE@fine-owl-12345.upstash.io",
        GROQ_API_KEY="",
        OPS_TOKEN="OPSVALUE",
    )

    said = preflight.check(wrong_everywhere, providers()).text()

    for value in ("SECRETVALUE", "DBPASSWORDVALUE", "REDISPASSWORDVALUE", "OPSVALUE", "dbuser", "fine-owl"):
        assert value not in said


def test_the_check_can_be_run_on_a_file_of_values_before_they_are_given_to_a_platform(tmp_path, capsys, monkeypatch):
    # What this shell has exported is not what the platform will be given: the file is judged on its own.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/dev")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("GROQ_API_KEY", "a-key-this-shell-has")
    values = tmp_path / "production.env"
    values.write_text(
        "\n".join(
            f"{name}={value}" for name, value in {**GOOD, **PROVIDERS, "REDIS_URL": "https://x.upstash.io"}.items()
        )
    )
    assert preflight.main(["--env-file", str(values)]) == 1
    printed = capsys.readouterr().out
    assert "[PREFLIGHT] APP_ENV=production" in printed and "REST API cannot subscribe" in printed

    values.write_text("\n".join(f"{name}={value}" for name, value in {**GOOD, **PROVIDERS}.items()))
    assert preflight.main(["--env-file", str(values)]) == 0
    assert "Every setting a deployment needs is there." in capsys.readouterr().out

    without_the_model_key = {name: value for name, value in {**GOOD, **PROVIDERS}.items() if name != "GROQ_API_KEY"}
    values.write_text("\n".join(f"{name}={value}" for name, value in without_the_model_key.items()))
    assert preflight.main(["--env-file", str(values)]) == 1  # the shell's key does not make up for the file's
    assert "GROQ_API_KEY is empty" in capsys.readouterr().out
    assert __import__("os").environ["GROQ_API_KEY"] == "a-key-this-shell-has"  # and the shell is left as it was

    # a development file is judged as production when asked: "would this deploy?"
    values.write_text("APP_ENV=development\nJWT_SECRET=change-me-before-production\n")
    assert preflight.main(["--env-file", str(values)]) == 0
    assert preflight.main(["--env-file", str(values), "--production"]) == 1


async def test_in_production_the_app_refuses_to_serve_from_settings_that_are_not_fit_to():
    from app import main

    connect = AsyncMock()
    unfit = {"APP_ENV": "production", "JWT_SECRET": "change-me-before-production"}
    with (
        patch.multiple(settings, **unfit),
        patch.multiple(main, _show_app_logs=MagicMock(), init_redis=connect, _recover_after_restart=AsyncMock()),
    ):
        with pytest.raises(preflight.NotReady, match="JWT_SECRET is the default from the repository"):
            async with main.lifespan(main.app):
                pass

    connect.assert_not_awaited()  # refused before anything was connected to


# ── Redis ──────────────────────────────────────────────────────────────────


async def test_a_closed_connection_is_retried_and_a_timeout_is_not():
    options = redis_db.connection_options()

    assert options["retry_on_error"] == [RedisConnectionError]  # a command that never left is sent again…
    retry = options["retry"]
    assert retry.get_retries() == redis_db.RECONNECT_ATTEMPTS == 3
    assert RedisTimeoutError not in options["retry_on_error"]  # …one that may have arrived is not: a PUBLISH twice
    assert options["health_check_interval"] == 30  # an idle connection is pinged before it is trusted
    assert options["socket_keepalive"] is True
    probes = options["socket_keepalive_options"]
    assert sorted(probes.values()) == [3, 10, 30]  # three probes, ten seconds apart, after thirty of silence

    with (
        patch.object(redis_db.aioredis, "from_url", return_value=MagicMock()) as connect,
        patch.object(settings, "REDIS_URL", UPSTASH),
    ):
        await redis_db.init_redis()
    assert connect.call_args.args == (UPSTASH,)  # rediss:// is all TLS takes
    assert connect.call_args.kwargs["retry_on_error"] == [RedisConnectionError]
    redis_db.redis_client = None


def test_a_hosted_redis_is_named_in_a_log_without_its_password(caplog):
    assert redis_db.where(UPSTASH) == "rediss://fine-owl-12345.upstash.io:6379"
    assert tool_cache._where(UPSTASH) == "rediss://fine-owl-12345.upstash.io:6379"
    assert redis_db.where("redis://localhost:6379") == "redis://localhost:6379"

    # the tools' cache used to log the URL as it stood, password and all
    connected = MagicMock(ping=MagicMock(return_value=True))
    with (
        patch.object(tool_cache, "_client", None),
        patch.object(tool_cache.mcp_settings, "REDIS_URL", UPSTASH),
        patch("redis.from_url", return_value=connected) as connect,
        caplog.at_level(logging.INFO, logger="src.ai.mcp_server.cache"),
    ):
        assert tool_cache._get_client() is connected

    assert "MCP cache: Redis connected at rediss://fine-owl-12345.upstash.io:6379" in caplog.text
    assert "AbCdUpstashPassw0rd" not in caplog.text
    assert connect.call_args.kwargs["health_check_interval"] == 30 and connect.call_args.kwargs["retry_on_error"]


# ── What the app answers ───────────────────────────────────────────────────


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


def a_redis(answers: bool) -> AsyncMock:
    return AsyncMock(ping=AsyncMock(return_value=True) if answers else AsyncMock(side_effect=ConnectionError("gone")))


@pytest.mark.parametrize(
    ("postgres_up", "redis_up", "status"), [(True, True, 200), (False, True, 503), (True, False, 503)]
)
async def test_ready_says_with_its_status_what_ping_says_in_its_body(postgres_up, redis_up, status):
    database = AsyncMock(side_effect=None if postgres_up else OSError("connection refused"))
    with (
        patch("app.api.routes.health.get_db_raw", database),
        patch("app.api.routes.health.get_redis", AsyncMock(return_value=a_redis(redis_up))),
    ):
        async with client() as http:
            ready, ping = await http.get("/ready"), await http.get("/ping")

    assert ready.status_code == status  # what a platform's health check reads
    assert ping.status_code == 200  # the roadmap's: errors in the body, never in the status
    assert ready.json() == ping.json()
    assert (ready.json()["postgres"] == "ok", ready.json()["redis"] == "ok") == (postgres_up, redis_up)


async def test_a_public_health_answer_in_production_names_the_kind_of_error_and_nothing_else(caplog):
    broken = AsyncMock(side_effect=OSError('connection to "db-internal-7.example" as user "app_rw" refused'))
    with (
        patch("app.api.routes.health.get_db_raw", broken),
        patch("app.api.routes.health.get_redis", AsyncMock(return_value=a_redis(True))),
    ):
        async with client() as http:
            on_a_laptop = (await http.get("/ping")).json()
            with patch.object(settings, "APP_ENV", "production"), caplog.at_level(logging.WARNING):
                in_production = (await http.get("/ping")).json()

    assert "db-internal-7.example" in on_a_laptop["postgres"]  # the whole message, where it helps
    assert in_production == {"postgres": "error: OSError", "redis": "ok"}  # anyone can ask this address
    assert "db-internal-7.example" in caplog.text  # the rest is in the log, for whoever runs it


async def test_every_answer_says_it_must_not_be_cached_unless_its_route_said_otherwise():
    async with client() as http:
        answer = await http.get("/ping")
        missing = await http.get("/no-such-route")
    assert answer.headers["cache-control"] == "no-store"
    assert missing.headers["cache-control"] == "no-store"  # errors too

    async def cacheable(_request):
        return PlainTextResponse("a logo", headers={"Cache-Control": "public, max-age=3600"})

    async def plain(_request):
        return PlainTextResponse("a trip")

    wrapped = NoStoreByDefault(Starlette(routes=[Route("/logo", cacheable), Route("/trip", plain)]))
    async with AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test") as http:
        assert (await http.get("/logo")).headers["cache-control"] == "public, max-age=3600"  # its route decides
        assert (await http.get("/trip")).headers.get_list("cache-control") == ["no-store"]


async def test_the_route_that_fails_on_purpose_is_only_there_for_whoever_holds_the_operations_token():
    token = "t" * 64
    async with client() as http:
        with patch.object(settings, "OPS_TOKEN", ""):
            switched_off = await http.get("/admin/sentry-test", headers={"X-Ops-Token": ""})
            also_off = await http.get("/admin/sentry-test", headers={"X-Ops-Token": token})
        with patch.object(settings, "OPS_TOKEN", token):
            no_token = await http.get("/admin/sentry-test")
            wrong = await http.get("/admin/sentry-test", headers={"X-Ops-Token": "t" * 63 + "x"})
            bad_marker = await http.get("/admin/sentry-test?marker=<script>", headers={"X-Ops-Token": token})
            failed = await http.get("/admin/sentry-test?marker=abc-123", headers={"X-Ops-Token": token})

    assert [r.status_code for r in (switched_off, also_off, no_token, wrong)] == [404, 404, 404, 404]
    assert no_token.json()["detail"] == "Not Found"  # as any address that does not exist
    assert bad_marker.status_code == 422
    assert failed.status_code == 500


async def test_an_error_nobody_caught_is_answered_in_the_usual_envelope_and_says_nothing_of_itself():
    token = "t" * 64
    with patch.object(settings, "OPS_TOKEN", token):
        async with client() as http:
            failed = await http.get("/admin/sentry-test?marker=inner-detail", headers={"X-Ops-Token": token})

    assert failed.status_code == 500
    body = failed.json()
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR" and body["detail"] == body["error"]["message"]
    assert "inner-detail" not in failed.text and "DeliberateError" not in failed.text


def test_origins_can_be_allowed_by_pattern_as_well_as_by_name():
    (cors,) = [m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
    assert "allow_origin_regex" in cors.kwargs and cors.kwargs["allow_origins"] == settings.cors_origins
    assert Settings.model_fields["CORS_ORIGIN_REGEX"].default == ""  # off unless asked for


# ── The files it is deployed with ──────────────────────────────────────────


def requirement_names(path: Path) -> list[str]:
    names = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.append(re.sub(r"[-_.]+", "-", re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0]).lower())
    return names


def pins() -> dict[str, str]:
    return {
        re.sub(r"[-_.]+", "-", name).lower(): version
        for name, version in (
            line.split("==", 1) for line in (ROOT / "constraints.txt").read_text().splitlines() if "==" in line
        )
    }


def test_what_is_deployed_is_what_was_tested():
    """The first image installed the newest of everything, and ran a SQLAlchemy no test here had ever seen."""
    pinned = pins()

    for file in ("requirements.txt", "requirements-dev.txt"):
        floating = [name for name in requirement_names(ROOT / file) if name not in pinned]
        assert not floating, f"{file}: not pinned in constraints.txt (python scripts/freeze_constraints.py): {floating}"
    # …and this environment — the one these tests are running in — is that pinned set
    different = {
        name: (metadata.version(name), pinned[name])
        for name in requirement_names(ROOT / "requirements.txt")
        if metadata.version(name) != pinned[name]
    }
    assert not different, (
        f"installed ≠ constraints.txt — run the suites, then scripts/freeze_constraints.py: {different}"
    )

    dockerfile = (ROOT / "src/backend/Dockerfile").read_text()
    assert "pip install --no-cache-dir -r requirements.txt -c constraints.txt" in dockerfile
    assert "-c constraints.txt" in (ROOT / ".github/workflows/ci.yml").read_text()  # CI tests what ships


def test_the_image_checks_migrates_and_then_becomes_the_server():
    dockerfile = (ROOT / "src/backend/Dockerfile").read_text()
    (command,) = re.findall(r'^CMD \["sh", "-c", "(.+)"\]$', dockerfile, flags=re.M)
    steps = [step.strip() for step in command.split("&&")]

    assert steps[0] == "python -m app.preflight"  # what is missing is said first, and all of it
    assert steps[1] == "alembic upgrade head"
    assert steps[2].startswith("exec uvicorn app.main:app")  # exec: the platform's stop signal reaches the server
    assert "--port ${PORT:-8000}" in steps[2] and "--host 0.0.0.0" in steps[2]  # where the platform says
    assert "--proxy-headers" in steps[2]
    assert re.search(r"^ENV APP_ENV=production", dockerfile, flags=re.M)  # an image is something that is deployed
    assert re.search(r"^USER app$", dockerfile, flags=re.M)  # and not root
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignored and ".env.*" in ignored  # no file of secrets goes into it


def test_the_platform_is_told_how_to_build_check_and_stop_it():
    config = json.loads((ROOT / "railway.json").read_text())

    assert config["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "src/backend/Dockerfile"}
    assert (ROOT / config["build"]["dockerfilePath"]).exists()
    deploy = config["deploy"]
    # the one whose status means something — and it is a route this app has
    assert deploy["healthcheckPath"] == "/ready" and "/ready" in app.openapi()["paths"]
    # The platform must wait longer before it kills than the server takes to stop by itself: its open
    # connections' grace, then the plans in flight, then giving up those that did not finish.
    closing = int(
        re.search(r"--timeout-graceful-shutdown (\d+)", (ROOT / "src/backend/Dockerfile").read_text()).group(1)
    )
    plans = Settings.model_fields["SHUTDOWN_GRACE_SECONDS"].default
    assert int(deploy["drainingSeconds"]) >= closing + plans + 10
    assert int(deploy["overlapSeconds"]) > 0


def test_the_production_like_stack_gives_the_backend_what_a_platform_would_and_nothing_else():
    compose = (ROOT / "docker/prodlike/compose.yml").read_text()
    backend = compose[compose.index("  backend:") : compose.index("\nvolumes:")]

    for variable in (
        "PORT",
        "DATABASE_URL",
        "REDIS_URL",
        "JWT_SECRET",
        "CORS_ORIGINS",
        "SENTRY_DSN",
        "OPS_TOKEN",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "DUFFEL_ACCESS_TOKEN",
        "LITEAPI_API_KEY",
        "OPENTRIPMAP_API_KEY",
    ):
        assert re.search(rf"^      {variable}:", backend, flags=re.M), variable
    assert "env_file" not in backend and "./src" not in backend  # no .env, no source mounted: the image as built
    assert "sslmode=require&channel_binding=require" in backend and "rediss://" in backend
    pooler = (ROOT / "docker/prodlike/pgbouncer.ini").read_text()
    assert "pool_mode = transaction" in pooler and "max_prepared_statements = 0" in pooler
    assert "hostnossl all  all  all            reject" in (ROOT / "docker/prodlike/pg_hba.conf").read_text()


def test_every_setting_is_in_the_example_file():
    named = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), flags=re.M))
    assert not set(Settings.model_fields) - named, sorted(set(Settings.model_fields) - named)
    assert not set(MCPSettings.model_fields) - named


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to load next.config.mjs")
def test_a_deployment_build_of_the_frontend_stops_without_the_backends_address():
    """Both addresses are compiled in when the site is built. A default there is a site that deploys and then
    sends every request to its own localhost."""

    def build_config(**env) -> subprocess.CompletedProcess:
        script = (
            "import('./next.config.mjs').then(async m => console.log(JSON.stringify(await m.default.rewrites())),"
            " e => { console.log('REFUSED ' + e.message); })"
        )
        return subprocess.run(
            ["node", "-e", script],
            cwd=ROOT / "src/frontend",
            env={"PATH": __import__("os").environ["PATH"], **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    on_a_laptop = build_config()
    assert json.loads(on_a_laptop.stdout)[0]["destination"] == "http://localhost:8000/:path*"

    refused = build_config(VERCEL="1").stdout
    assert refused.startswith("REFUSED Cannot build for deployment")
    assert "BACKEND_URL is not set" in refused and "NEXT_PUBLIC_API_URL is not set" in refused
    half = build_config(
        VERCEL="1", BACKEND_URL="http://api.example.com", NEXT_PUBLIC_API_URL="https://localhost:8000"
    ).stdout
    assert "BACKEND_URL must be an https:// address" in half and "NEXT_PUBLIC_API_URL points at localhost" in half

    deployed = build_config(
        VERCEL="1", BACKEND_URL="https://api.example.com/", NEXT_PUBLIC_API_URL="https://api.example.com"
    )
    assert json.loads(deployed.stdout) == [{"source": "/api/:path*", "destination": "https://api.example.com/:path*"}]
