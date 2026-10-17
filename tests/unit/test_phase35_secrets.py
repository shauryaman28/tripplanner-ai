"""Phase 35 — no key in a log line, in the repository, or in its history.

The LogRedactor (src/telemetry/logs.py) on every record a process makes, in text as in JSON; the
scrubber's key shapes; and scripts/secret_audit.py, the search that says whether a key is anywhere it
should not be — held here to finding what is planted for it, and to never printing what it finds.
"""

import importlib.util
import io
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.error_reporting import FILTERED, Scrubber
from src.telemetry import logs

ROOT = Path(__file__).resolve().parents[2]
# keys made up for the tests, each in its provider's shape
GOOGLE_OLD = "AIza" + "S" * 35
GOOGLE_NEW = "AQ.Ab8RN6" + "x" * 44
GROQ = "gsk_" + "a1B2c3D4" * 7
DUFFEL = "duffel_test_" + "Zq9" * 15
LANGSMITH = "lsv2_pt_" + "0123456789abcdef" * 2 + "_0123456789"
LITEAPI = "sand_c0155ab8-1f2e-4d3c-9b8a-7f6e5d4c3b2a"
OWM = "a8a2" + "0f" * 14  # 32 hex: bare, like OpenWeather's
SETTING_VALUE = "the-value-of-a-secret-setting-42"


def audit_module():
    spec = importlib.util.spec_from_file_location("secret_audit", ROOT / "scripts" / "secret_audit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["secret_audit"] = module
    spec.loader.exec_module(module)
    return module


audit = audit_module()


def a_text_logger(scrub) -> tuple[logging.Logger, io.StringIO]:
    """A logger as a development console has one: plain text, a handler of its own — and the redactor in front."""
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(logs.LogRedactor(scrub))
    logger = logging.getLogger(f"phase35.{id(out)}")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    return logger, out


# ── The LogRedactor ────────────────────────────────────────────────────────


def test_a_text_log_line_loses_every_kind_of_secret_and_keeps_the_rest():
    scrub = Scrubber([SETTING_VALUE]).text
    logger, out = a_text_logger(scrub)
    logger.warning(
        "Client error for url 'https://api.openweathermap.org/data/2.5/forecast?q=Goa,IN&appid=%s&units=metric'", OWM
    )
    logger.warning("the setting: %s", SETTING_VALUE)
    logger.warning("keys seen: %s %s %s %s %s %s", GOOGLE_OLD, GOOGLE_NEW, GROQ, DUFFEL, LANGSMITH, LITEAPI)
    logger.warning("login token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLWJ5dGVz for asha@example.com")
    logger.info("[CACHE MISS] mcp:flights:0f1e2d3c4b5a69788796a5b4c3d2e1f0 · trace 5b8e0d7f2c1a4e6b9d3f0a2c4e6b8d0f")

    text = out.getvalue()
    for secret in (OWM, SETTING_VALUE, GOOGLE_OLD, GOOGLE_NEW, GROQ, DUFFEL, LANGSMITH, LITEAPI, "asha@example.com"):
        assert secret not in text, secret
    assert text.count(FILTERED) >= 10
    assert "appid=[Filtered]&units=metric" in text  # where it was, and that it was there
    # hex that is not a key — a cache key's digest, a trace id — is not touched
    assert "mcp:flights:0f1e2d3c4b5a69788796a5b4c3d2e1f0" in text and "5b8e0d7f2c1a4e6b9d3f0a2c4e6b8d0f" in text


def test_a_traceback_loses_them_too():
    logger, out = a_text_logger(Scrubber([SETTING_VALUE]).text)
    try:
        raise RuntimeError(f"provider refused key {GROQ} and {SETTING_VALUE}")
    except RuntimeError:
        logger.exception("the search failed")
    text = out.getvalue()
    assert "Traceback" in text and "RuntimeError: provider refused key [Filtered] and [Filtered]" in text
    assert GROQ not in text and SETTING_VALUE not in text


def test_every_record_of_the_process_is_redacted_as_it_is_made_whatever_handler_writes_it():
    from app import main  # noqa: F401 — the app installs it as it is loaded

    out = io.StringIO()
    handler = logging.StreamHandler(out)  # a library's own handler: no filter of ours on it
    library = logging.getLogger("some.library")
    library.addHandler(handler)
    try:
        library.warning("GET https://api.opentripmap.com/0.1/en/places/radius?apikey=%s", OWM)
    finally:
        library.removeHandler(handler)
    assert OWM not in out.getvalue() and "apikey=[Filtered]" in out.getvalue()


def test_installing_it_twice_replaces_the_scrubber_and_adds_no_second_one():
    first = logs.redact_every_record(Scrubber([]).text)
    factory = logging.getLogRecordFactory()
    second = logs.redact_every_record(Scrubber([SETTING_VALUE]).text)
    assert first is second and logging.getLogRecordFactory() is factory
    from app import main

    logs.redact_every_record(main._scrub)  # back as the app left it


def test_both_processes_install_it_before_anything_logs():
    main_source = (ROOT / "src/backend/app/main.py").read_text()
    assert main_source.index("logs.redact_every_record(_scrub)") < main_source.index("logs.configure(")
    from src.ai.mcp_server import server

    with (
        patch.object(server.logs, "redact_every_record") as installed,
        patch.object(server.logs, "configure"),
        patch.object(server.traces, "configure"),
    ):
        server._as_a_process()
    installed.assert_called_once()


def test_a_record_whose_arguments_do_not_fit_is_left_for_logging_to_report():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s and %s", ("only one",), None)
    assert logs.LogRedactor(Scrubber([]).text).filter(record) is True


# ── The scrubber and the audit agree on what a key looks like ──────────────


@pytest.mark.parametrize("key", [GOOGLE_OLD, GOOGLE_NEW, GROQ, DUFFEL, LANGSMITH, LITEAPI, "sk-ant-" + "k" * 30])
def test_whatever_the_audit_would_find_by_its_shape_the_scrubber_takes_out(key):
    line = f"something said {key} in passing"
    assert list(audit.scan_text(line, "x", {})), key  # the audit finds it…
    assert list(audit.scan_text(Scrubber([]).text(line), "x", {})) == []  # …and finds nothing once scrubbed


# ── The audit ──────────────────────────────────────────────────────────────


@pytest.fixture
def a_checkout(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=published_default_pw",
                f"GROQ_API_KEY={GROQ}",
                f"OPENWEATHER_API_KEY={OWM}",
                "DATABASE_URL=postgresql://u:a-database-password@db:5432/x",
                "GOOGLE_API_KEY=standin-not-a-key",
                "JWT_ALGORITHM=HS256",
            ]
        )
        + "\n"
    )
    (tmp_path / ".env.example").write_text("POSTGRES_PASSWORD=published_default_pw\n")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    return tmp_path


def test_the_audit_knows_every_secret_of_the_env_files_but_not_the_published_defaults(a_checkout):
    secrets = audit.secrets_from_env_files()
    assert set(secrets) == {GROQ, OWM, "a-database-password"}
    assert secrets[OWM] == "OPENWEATHER_API_KEY (.env)" and "DATABASE_URL" in secrets["a-database-password"]


def test_the_audit_finds_a_planted_key_by_value_and_by_shape_and_never_prints_it(a_checkout, capsys, monkeypatch):
    logs_dir = a_checkout / "logs"
    logs_dir.mkdir()
    (logs_dir / "backend.log").write_text(
        f"INFO fine\nWARNING GET https://api.openweathermap.org/data/2.5/forecast?appid={OWM}\n"
        f"ERROR an old key {GOOGLE_OLD} turned up\n"
    )
    monkeypatch.setattr(sys, "argv", ["secret_audit.py", "logs", str(logs_dir)])
    assert audit.main() == 1
    printed = capsys.readouterr().out
    assert "logs/backend.log:2  OPENWEATHER_API_KEY (.env)  (a8a2…)" in printed
    assert "logs/backend.log:3  a Google API key (AIza…)  (AIza…)" in printed
    assert OWM not in printed and GOOGLE_OLD not in printed
    assert "2 found" in printed


def test_the_audit_says_nothing_found_and_exits_0_when_there_is_nothing(a_checkout, capsys, monkeypatch):
    (a_checkout / "clean.log").write_text(
        "INFO a plan in 11.2 s\n[CACHE MISS] mcp:flights:0f1e2d3c4b5a69788796a5b4c3d2e1f0\n"
    )
    monkeypatch.setattr(sys, "argv", ["secret_audit.py", "logs", str(a_checkout / "clean.log")])
    assert audit.main() == 0 and "nothing found" in capsys.readouterr().out


def test_the_audit_reads_every_version_of_every_file_in_the_history(a_checkout):
    def git(*args):
        subprocess.run(["git", *args], cwd=a_checkout, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (a_checkout / "config.py").write_text(f'KEY = "{GROQ}"\n')  # committed once…
    git("add", "config.py")
    git("commit", "-qm", "oops")
    (a_checkout / "config.py").write_text("KEY = os.environ['GROQ_API_KEY']\n")  # …and taken out again
    git("commit", "-qam", "fixed")

    findings, commits, versions = audit.audit_repo(audit.secrets_from_env_files())
    assert commits == 2 and versions >= 2
    assert [finding.what for finding in findings] == [
        "GROQ_API_KEY (.env)"
    ]  # the current file is clean; history is not
    assert findings[0].where.startswith("history ") and ":config.py:1" in findings[0].where
