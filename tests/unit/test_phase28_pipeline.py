"""Phase 28 — the pipelines: what runs on a push, what a merge deploys, what the night reports.

A workflow is a file that only GitHub runs, so a mistake in one is found by pushing it and waiting
— or not found, when the mistake is that something no longer runs at all. These tests read the
three workflow files and hold what each must do: which events start it, which commands it runs and
in what order, what it may not do without, and what it must never do (deploy a commit whose tests
failed; post nothing on a night when the tests crashed).

The scripts the workflows call are ordinary programs and are tested as that: the wait for a deploy
to be live, the night's summary, and the posting of it — the last against a stand-in for GitHub's
CLI (tests/ci/fake_gh.sh) that writes down what it was asked.

That the files do what they say on a runner is the other half: docs/phase28_build_log.md — the
local runner, and GitHub itself.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from importlib import util
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from app import __version__
from app.core import error_reporting
from app.core.config import Settings, settings
from app.main import app

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text())
    loaded["on"] = loaded.pop(True)  # YAML reads the key `on` as the boolean true
    return loaded


def script(name: str):
    """A file from scripts/ as a module — they are programs, not a package."""
    spec = util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def commands(job: dict) -> list[str]:
    """What a job runs. (A job that is another workflow has no steps of its own.)"""
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def all_jobs() -> dict[str, dict]:
    return {
        f"{file.name}:{name}": job
        for file in sorted(WORKFLOWS.glob("*.yml"))
        for name, job in workflow(file.name)["jobs"].items()
    }


CI, DEPLOY, NIGHTLY = workflow("ci.yml"), workflow("deploy.yml"), workflow("nightly.yml")

# How GitHub runs a `run:` step: the first command that fails ends the script.
GITHUBS_SHELL = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]


# ── CI: what a push is asked ───────────────────────────────────────────────


def test_every_push_is_checked_and_a_pull_request_is_not_checked_a_second_time():
    assert CI["on"] == {"push": {"branches": ["**"]}}  # every branch; a pull request's commits are pushes
    # Not `pull_request` as well: it ran everything twice, and left two checks of one name on a commit —
    # one of which can be "skipped", which a required check counts as passed.
    assert "pull_request" not in CI["on"]
    # a newer push replaces the run of the older one — except on main, where the last step is a deploy
    assert CI["concurrency"] == {
        "group": "ci-${{ github.ref }}",
        "cancel-in-progress": "${{ github.ref != 'refs/heads/main' }}",
    }
    assert CI["permissions"] == {"contents": "read"}


def test_the_backend_job_lints_checks_the_migrations_and_runs_the_tests_on_the_versions_that_ship():
    backend = CI["jobs"]["backend"]
    ran = commands(backend)

    install, lint, alert_rules, migrations, tests = ran
    assert install == "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt"
    assert lint == "ruff check . && ruff format --check ."  # the roadmap's `ruff check src/`, and the rest of the repo
    assert tests == "pytest tests/unit/ tests/contract/ -v"  # the roadmap's, word for word
    # every migration applies; the models say nothing the migrations do not; and they come down and go up again
    assert migrations.split() == [
        *"alembic upgrade head".split(),
        *"alembic check".split(),
        *"alembic downgrade base".split(),
        *"alembic upgrade head".split(),
    ]
    # …against a real Postgres that has pgvector: the first migration creates the extension
    database = backend["services"]["postgres"]
    assert database["image"] == "pgvector/pgvector:pg16" and "--health-cmd" in database["options"]
    (step,) = [s for s in backend["steps"] if s.get("run") == migrations]
    assert step["env"]["DATABASE_URL"] == "postgresql://tripplanner:ci-only-password@localhost:5432/tripplanner_ci"
    assert database["env"] == {
        "POSTGRES_USER": "tripplanner",
        "POSTGRES_PASSWORD": "ci-only-password",
        "POSTGRES_DB": "tripplanner_ci",
    }
    # lint before anything slow: the cheapest failure first
    assert ran.index(lint) < ran.index(migrations) < ran.index(tests)
    # Phase 34: the alert rules, tested by promtool (tests/unit/test_phase34_alerts.py says what it runs)
    assert "--entrypoint promtool" in alert_rules and "test rules" in alert_rules
    assert ran.index(lint) < ran.index(alert_rules) < ran.index(migrations)


def test_the_frontend_job_checks_types_lints_and_builds_what_the_lock_file_says():
    frontend = CI["jobs"]["frontend"]

    assert frontend["defaults"]["run"]["working-directory"] == "src/frontend"
    assert commands(frontend) == ["npm ci", "npx tsc --noEmit", "npm run lint", "npm run build"]
    (node,) = [s for s in frontend["steps"] if "setup-node" in s.get("uses", "")]
    assert node["with"] == {
        "node-version": "22",
        "cache": "npm",
        "cache-dependency-path": "src/frontend/package-lock.json",
    }
    assert (ROOT / "src/frontend/package-lock.json").exists()  # `npm ci` installs exactly what it says


def test_a_merge_is_deployed_only_after_both_jobs_have_passed_and_only_from_main():
    deploy = CI["jobs"]["deploy"]

    assert deploy["needs"] == ["backend", "frontend"]  # a commit that fails a test is never deployed
    assert deploy["if"] == "github.ref == 'refs/heads/main'"
    assert deploy["uses"] == "./.github/workflows/deploy.yml" and deploy["secrets"] == "inherit"
    assert set(CI["jobs"]) == {"backend", "frontend", "deploy"}  # two checks to require on main, and the deploy
    # nothing else in the repository deploys
    elsewhere = [name for name, job in all_jobs().items() if "railway" in json.dumps(job) and "deploy.yml" not in name]
    assert elsewhere == []


# ── Deploy ─────────────────────────────────────────────────────────────────


def test_the_deploy_tells_the_deployment_its_commit_deploys_asks_for_its_status_and_waits_until_it_is_live():
    assert DEPLOY["on"] == {"workflow_call": None, "workflow_dispatch": None}  # by CI on main, or by hand
    assert DEPLOY["concurrency"] == {
        "group": "deploy-production",
        "cancel-in-progress": False,
    }  # none thrown away half-way
    job = DEPLOY["jobs"]["backend"]
    asked, install, say_commit, up, status, wait = commands(job)

    assert "RAILWAY_TOKEN" in asked and "RAILWAY_SERVICE" in asked
    assert "npm install --global @railway/cli" in install
    assert say_commit == 'railway variable set "GIT_COMMIT_SHA=$GITHUB_SHA" --service "$RAILWAY_SERVICE" --skip-deploys'
    assert up == 'railway up --ci --service "$RAILWAY_SERVICE"'  # --ci: exit 1 when the deploy fails
    assert status == "railway status"  # the roadmap's check
    assert wait.split() == [
        *"python3 scripts/wait_for_release.py".split(),
        *'--commit "$GITHUB_SHA" --api "$PRODUCTION_API_URL" --web "$PRODUCTION_WEB_URL"'.split(),
        *'--since "${{ github.event.head_commit.timestamp }}" --timeout 600'.split(),
    ]
    assert job["env"] == {
        "RAILWAY_TOKEN": "${{ secrets.RAILWAY_TOKEN }}",
        "RAILWAY_SERVICE": "${{ vars.RAILWAY_SERVICE }}",
        "PRODUCTION_API_URL": "${{ vars.PRODUCTION_API_URL }}",
        "PRODUCTION_WEB_URL": "${{ vars.PRODUCTION_WEB_URL }}",
    }
    assert "echo $RAILWAY_TOKEN" not in json.dumps(job) and 'echo "$RAILWAY_TOKEN' not in json.dumps(job)


def test_a_repository_with_nowhere_to_deploy_to_can_still_merge(tmp_path):
    """Before the accounts exist, every merge to main reaches this job. It must say so and pass — not fail
    every merge, and not go through the motions of a deploy with an empty token."""
    job = DEPLOY["jobs"]["backend"]
    first, *rest = job["steps"]

    assert first["id"] == "target" and "configured=false" in first["run"] and "configured=true" in first["run"]
    assert "::notice title=Nothing was deployed::" in first["run"]
    for step in rest:  # every step after the first only runs when there is a target
        assert step["if"] == "steps.target.outputs.configured == 'true'", step.get("name") or step.get("uses")

    def answer(**env) -> tuple[int, str, str]:
        output = tmp_path / "github-output"
        output.write_text("")
        done = subprocess.run(
            [*GITHUBS_SHELL, first["run"]],
            env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **env},
            capture_output=True,
            text=True,
        )
        return done.returncode, done.stdout, output.read_text()

    code, said, out = answer(RAILWAY_TOKEN="", RAILWAY_SERVICE="")
    assert code == 0 and "Nothing was deployed" in said and out.strip() == "configured=false"
    assert answer(RAILWAY_TOKEN="a-token", RAILWAY_SERVICE="")[2].strip() == "configured=false"  # half is not enough
    code, said, out = answer(RAILWAY_TOKEN="a-token", RAILWAY_SERVICE="backend")
    assert code == 0 and said == "" and out.strip() == "configured=true"


# ── Nightly ────────────────────────────────────────────────────────────────


def test_every_night_at_two_the_integration_tests_run_on_real_services():
    assert NIGHTLY["on"]["schedule"] == [{"cron": "0 2 * * *"}]  # the roadmap's
    assert "workflow_dispatch" in NIGHTLY["on"]
    # a schedule only runs what is on main: a branch of this name is how a change to the job is tried first
    assert NIGHTLY["on"]["push"] == {"branches": ["nightly-check/**"]}
    assert NIGHTLY["permissions"] == {"contents": "read", "issues": "write"}  # to post, and nothing more

    job = NIGHTLY["jobs"]["tests"]
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert job["services"]["redis"]["image"] == "redis:7-alpine"
    assert job["env"] == {
        "DATABASE_URL": "postgresql://tripplanner:ci-only-password@localhost:5432/tripplanner_ci",
        "REDIS_URL": "redis://localhost:6379",
        "RUN_INTEGRATION": "1",
    }
    steps = {step["name"]: step for step in job["steps"] if "name" in step}
    assert steps["Integration tests"]["run"] == "pytest tests/integration/ -v --junitxml=reports/integration.xml"
    # Phase 33: the eval suite itself — real plans and a real grader, with the keys as repository secrets
    evals = steps["Eval suite (Phase 33)"]
    assert "python scripts/run_eval.py --assert-min-score 3.5" in evals["run"]
    assert "--junit reports/evals.xml" in evals["run"] and "--no-history" in evals["run"]
    assert "|| verdict=$?" in evals["run"] and "exit $verdict" in evals["run"]  # the table reaches the page either way
    assert evals["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert set(evals["env"]) >= {"GROQ_API_KEY", "DUFFEL_ACCESS_TOKEN", "LITEAPI_API_KEY", "OPENTRIPMAP_API_KEY"}
    assert job["timeout-minutes"] == 35 and evals["timeout-minutes"] == 25  # the suite cannot use up the job


def test_the_night_is_reported_whatever_happened_and_a_bad_night_is_a_failed_run():
    job = NIGHTLY["jobs"]["tests"]
    steps = {step["name"]: step for step in job["steps"] if "name" in step}
    order = [step.get("name") for step in job["steps"]]

    # a suite that fails does not stop the night: the comment has to be written either way
    assert steps["Integration tests"]["continue-on-error"] is True
    assert steps["Eval suite (Phase 33)"]["continue-on-error"] is True
    summary = steps["Write the night's summary"]
    assert summary["id"] == "summary" and summary["continue-on-error"] is True
    assert "--require integration" in summary["run"]  # no report from them is a failed night, not an empty one
    assert "--require evals" in summary["run"] and "--absent" not in summary["run"]  # Phase 33: it exists
    assert "exit $verdict" in summary["run"] and 'cat summary.md >> "$GITHUB_STEP_SUMMARY"' in summary["run"]

    post = steps["Post it to the nightly issue"]
    assert post["if"] == "always()" and post["run"] == "bash scripts/nightly_post.sh summary.md"
    assert post["env"] == {"GH_TOKEN": "${{ github.token }}", "GH_REPO": "${{ github.repository }}"}

    verdict = steps["A night in which something failed is a failed run"]
    assert verdict["if"] == "steps.summary.outcome != 'success'" and "exit 1" in verdict["run"]
    assert order.index("Post it to the nightly issue") < order.index(
        "A night in which something failed is a failed run"
    )


def test_the_summary_step_as_github_runs_it_puts_a_bad_night_on_the_runs_page_too(tmp_path):
    """GitHub's shell stops a script at the first command that fails — and on a bad night that command is the
    one that writes the summary. What comes after it (the run's page, the verdict) has to happen anyway."""
    step = {s["name"]: s for s in NIGHTLY["jobs"]["tests"]["steps"] if "name" in s}["Write the night's summary"]
    (tmp_path / "scripts").symlink_to(ROOT / "scripts")
    page = tmp_path / "page.md"

    def night() -> tuple[int, str]:
        page.write_text("")
        done = subprocess.run(
            [*GITHUBS_SHELL, step["run"]],
            cwd=tmp_path,
            env={
                "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
                "GITHUB_STEP_SUMMARY": str(page),
                "GITHUB_SHA": COMMIT,
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "o/r",
                "GITHUB_RUN_ID": "1",
            },
            capture_output=True,
            text=True,
        )
        assert (tmp_path / "summary.md").read_text() == page.read_text(), done.stderr  # what is posted is what is shown
        return done.returncode, page.read_text()

    code, said = night()  # the tests never started: no report
    assert code == 1 and "DID NOT RUN — no report was written" in said

    junit(tmp_path / "reports", "integration", passed=60)
    code, said = night()  # Phase 33: the eval suite's report is required too
    assert code == 1 and "| evals |" in said and "DID NOT RUN" in said

    junit(tmp_path / "reports", "evals", passed=17)
    code, said = night()
    assert code == 0 and "— 77 passed" in said and "[the run](https://github.com/o/r/actions/runs/1)" in said


# ── All three ──────────────────────────────────────────────────────────────


def test_every_job_has_a_time_limit_and_no_workflow_may_write_to_the_repository():
    for name, job in all_jobs().items():
        if "uses" in job:  # a job that is another workflow: its own jobs carry the limits
            continue
        # a hung job is stopped, not billed for six hours — the nightly allows the eval suite's real plans (Phase 33)
        assert 0 < job["timeout-minutes"] <= (35 if name == "nightly.yml:tests" else 20), name
        assert job["runs-on"] == "ubuntu-latest", name
    for file in WORKFLOWS.glob("*.yml"):
        permissions = workflow(file.name)["permissions"]
        assert permissions["contents"] == "read", file.name
        assert set(permissions) <= {"contents", "issues"}, file.name


def test_only_githubs_own_actions_are_used_each_at_a_named_version():
    used = sorted({step["uses"] for job in all_jobs().values() for step in job.get("steps", []) if "uses" in step})
    assert used == [
        "actions/checkout@v4",
        "actions/setup-node@v4",
        "actions/setup-python@v5",
        "actions/upload-artifact@v4",
    ]


def test_the_pipelines_run_on_the_python_and_the_versions_that_are_deployed():
    image = re.search(r"^FROM python:(\d+\.\d+)", (ROOT / "src/backend/Dockerfile").read_text(), flags=re.M).group(1)
    pythons = {
        step["with"]["python-version"]
        for job in all_jobs().values()
        for step in job.get("steps", [])
        if "setup-python" in step.get("uses", "")
    }
    assert pythons == {image} == {"3.11"}
    installs = [run for job in all_jobs().values() for run in commands(job) if run.startswith("pip install")]
    assert len(installs) == 2 and all(run.endswith("-c constraints.txt") for run in installs)


def test_everything_a_workflow_runs_from_the_repository_is_in_the_repository():
    text = "\n".join(file.read_text() for file in WORKFLOWS.glob("*.yml"))
    named = set(re.findall(r"\b(scripts/[\w./-]+\.(?:py|sh))", text)) | set(
        re.findall(r"(\./\.github/workflows/[\w.-]+)", text)
    )
    assert named == {
        "scripts/wait_for_release.py",
        "scripts/nightly_summary.py",
        "scripts/nightly_post.sh",
        "scripts/run_eval.py",
        "./.github/workflows/deploy.yml",
    }
    for path in named:
        assert (ROOT / path).exists(), path
    assert (ROOT / ".github/nightly-issue.md").exists()  # what the nightly issue is opened with


# ── What is running: GET /version ──────────────────────────────────────────


async def test_a_deployment_says_which_commit_it_is_running():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        with patch.object(settings, "GIT_COMMIT_SHA", ""), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RAILWAY_GIT_COMMIT_SHA", None)
            on_a_laptop = await http.get("/version")
        with patch.object(settings, "GIT_COMMIT_SHA", "3f2a9c1d5e6b7a8091a2b3c4d5e6f708192a3b4c"):
            deployed = await http.get("/version")
            release = error_reporting.release()

    assert on_a_laptop.json() == {"version": __version__, "commit": None}  # nobody said
    assert deployed.json() == {"version": __version__, "commit": "3f2a9c1d5e6b7a8091a2b3c4d5e6f708192a3b4c"}
    assert deployed.headers["cache-control"] == "no-store"  # a deploy is waited for by asking this again and again
    assert release == f"tripplanner-ai@{__version__}+3f2a9c1d5e6b"  # and an error report names the same commit
    assert "/version" in app.openapi()["paths"]


def test_the_commit_is_the_pipelines_or_the_platforms_own():
    ours = Settings(_env_file=None, GIT_COMMIT_SHA="ours")
    nobodys = Settings(_env_file=None, GIT_COMMIT_SHA="")
    with patch.dict(os.environ, {"RAILWAY_GIT_COMMIT_SHA": "railways"}):
        assert ours.commit == "ours"  # what the pipeline set wins
        assert nobodys.commit == "railways"  # a platform that builds from GitHub itself says it under its own name
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RAILWAY_GIT_COMMIT_SHA", None)
        assert nobodys.commit == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to load next.config.mjs")
def test_the_site_is_built_knowing_its_commit():
    def built_with(**env) -> str:
        done = subprocess.run(
            ["node", "-e", "import('./next.config.mjs').then(m => console.log(JSON.stringify(m.default.env)))"],
            cwd=ROOT / "src/frontend",
            env={"PATH": os.environ["PATH"], **env},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(done.stdout)["NEXT_PUBLIC_COMMIT_SHA"]

    assert built_with() == ""  # a laptop's build
    assert built_with(VERCEL_GIT_COMMIT_SHA="abc1234") == "abc1234"  # Vercel says it when it builds from GitHub
    assert built_with(GIT_COMMIT_SHA="def5678") == "def5678"
    route = (ROOT / "src/frontend/src/app/version/route.ts").read_text()
    assert "process.env.NEXT_PUBLIC_COMMIT_SHA || null" in route and 'dynamic = "force-static"' in route


# ── Waiting for a deploy to be live ────────────────────────────────────────

release = script("wait_for_release")
COMMIT = "3f2a9c1d5e6b7a8091a2b3c4d5e6f708192a3b4c"


class Clock:
    """Time that only passes when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_short_sha_and_a_long_one_are_the_same_commit():
    assert release.same_commit(COMMIT, COMMIT)
    assert release.same_commit(COMMIT[:7], COMMIT) and release.same_commit(COMMIT, COMMIT[:12])
    assert not release.same_commit("0000000", COMMIT)
    assert not release.same_commit(COMMIT[:6], COMMIT)  # six characters is not enough to say
    assert not release.same_commit(None, COMMIT) and not release.same_commit("", COMMIT)


def test_three_addresses_are_asked_the_way_a_browser_reaches_them():
    assert release.addresses("https://api.example.com/", "https://app.example.com") == {
        "the backend": "https://api.example.com/version",
        "the site": "https://app.example.com/version",
        "the site's /api": "https://app.example.com/api/version",  # the site reaches a backend that is running it too
    }
    assert release.addresses("https://api.example.com", None) == {"the backend": "https://api.example.com/version"}
    assert release.addresses(None, None) == {}


def test_the_wait_ends_when_every_address_answers_with_the_commit_and_not_before():
    clock = Clock()
    goes_live_at = {"https://api/version": 20.0, "https://app/version": 45.0, "https://app/api/version": 20.0}
    asked: list[tuple[float, str]] = []

    def fetch(url: str) -> dict:
        asked.append((clock.now, url))
        if url == "https://app/version" and clock.now < 10:
            raise ConnectionError("the site is being rebuilt")  # an address that does not answer yet is asked again
        return {"commit": COMMIT if clock.now >= goes_live_at[url] else "0ld0ld0ld0ld"}

    live, last = release.wait(release.addresses("https://api", "https://app"), COMMIT, 600, fetch, clock.sleep, clock)

    assert live == {"the backend": 20.0, "the site's /api": 20.0, "the site": 45.0}
    assert clock.now == 45.0  # it stopped the moment the last one was live
    # an address that has answered with the commit is not asked again
    assert max(when for when, url in asked if url == "https://api/version") == 20.0
    assert last["the site"].startswith("commit 3f2a9c1d5e6b")


def test_a_deploy_that_never_goes_live_is_given_up_with_what_each_address_last_said():
    clock = Clock()

    def fetch(url: str) -> dict:
        if "app" in url:
            raise TimeoutError("timed out")
        return {"commit": "0ld0ld0ld0ld", "version": "0.27.0"}

    live, last = release.wait(release.addresses("https://api", "https://app"), COMMIT, 30, fetch, clock.sleep, clock)

    assert live == {} and clock.now == 30.0  # no longer than it was told to wait
    assert last == {
        "the backend": "commit 0ld0ld0ld0ld",
        "the site": "TimeoutError: timed out",
        "the site's /api": "TimeoutError: timed out",
    }


def test_the_wait_says_how_long_merge_to_live_took_and_warns_past_five_minutes(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    live_at_once = {"commit": COMMIT}
    arguments = ["--commit", COMMIT, "--api", "https://api", "--web", "https://app"]

    with patch.object(release, "fetch_json", lambda _url: live_at_once), patch.object(release.time, "sleep"):
        with patch.object(release, "seconds_since", lambda _since: 212.0):
            assert release.main([*arguments, "--since", "2026-10-11T12:00:00Z"]) == 0
        quick = capsys.readouterr().out
        with patch.object(release, "seconds_since", lambda _since: 371.0):
            assert release.main([*arguments, "--since", "2026-10-11T12:00:00Z"]) == 0  # live is live: not a failure
        slow = capsys.readouterr().out

    assert "From the merge to live: **212 s** (the aim: within 300 s)." in quick and "::warning::" not in quick
    assert "::warning::Merge to live took 371 s — longer than the 300 s aimed for." in slow
    assert "**3f2a9c1 is live**" in (tmp_path / "summary.md").read_text()  # on the run's page

    assert release.seconds_since("2026-10-11T12:00:00Z", now=1791720060.0) == pytest.approx(60.0)
    assert release.seconds_since("", now=0) is None and release.seconds_since("not a time", now=0) is None


def test_the_wait_fails_the_job_when_the_deploy_is_not_live_and_has_nothing_to_do_without_an_address(
    capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    old = {"commit": "0ld0ld0ld0ld"}

    with patch.object(release, "fetch_json", lambda _url: old), patch.object(release, "INTERVAL_S", 0.0):
        with patch.object(release.time, "sleep"):
            code = release.main(["--commit", COMMIT, "--api", "https://api", "--timeout", "0"])
    said = capsys.readouterr().out
    assert code == 1
    assert "::error::the backend is not running 3f2a9c1 after 0 s — last answer: commit 0ld0ld0ld0ld" in said

    assert release.main(["--commit", COMMIT, "--api", "", "--web", ""]) == 0  # the addresses were never configured
    assert "::notice::No address to ask" in capsys.readouterr().out


# ── The night's summary ────────────────────────────────────────────────────

nightly = script("nightly_summary")


def junit(
    directory: Path, name: str, passed: int = 0, failed: tuple[tuple[str, str], ...] = (), skipped: int = 0, seconds=1.5
):
    cases = [f'<testcase classname="tests.integration.test_x" name="test_ok_{n}" time="0.1"/>' for n in range(passed)]
    cases += [
        f'<testcase classname="tests.integration.test_x" name="{test}"><failure message="{why}">trace\nback</failure></testcase>'
        for test, why in failed
    ]
    cases += [
        f'<testcase classname="tests.integration.test_x" name="test_skipped_{n}"><skipped/></testcase>'
        for n in range(skipped)
    ]
    total = passed + len(failed) + skipped
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.xml").write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{total}" failures="{len(failed)}" '
        f'errors="0" skipped="{skipped}" time="{seconds}">{"".join(cases)}</testsuite></testsuites>'
    )


def summary(directory: Path, *arguments: str, capsys) -> tuple[int, str]:
    code = nightly.main([str(directory), *arguments])
    return code, capsys.readouterr().out


def test_a_good_night_is_one_line_a_table_and_where_to_look(tmp_path, capsys):
    junit(tmp_path, "integration", passed=58, skipped=2, seconds=31.4)

    with patch.object(nightly, "datetime") as now:
        now.now.return_value.strftime.return_value = "2026-10-12"
        code, said = summary(
            tmp_path,
            "--require", "integration",
            "--absent", "evals=no eval suite yet (Phase 33)",
            "--commit", COMMIT,
            "--run-url", "https://github.com/o/r/actions/runs/1",
            capsys=capsys,
        )  # fmt: skip

    assert code == 0
    assert said.splitlines() == [
        "### ✅ Nightly, 2026-10-12 — 58 passed",
        "",
        "| Suite | Passed | Failed | Skipped | Took |",
        "|---|---|---|---|---|",
        "| integration | 58 | 0 | 2 | 31 s |",
        "| evals | — | — | — | not run: no eval suite yet (Phase 33) |",
        "",
        "Commit `3f2a9c1` · [the run](https://github.com/o/r/actions/runs/1)",
    ]


def test_a_bad_night_names_what_failed_and_why(tmp_path, capsys):
    junit(
        tmp_path,
        "integration",
        passed=57,
        failed=(
            ("test_plan_then_refine_end_to_end", "assert 500 == 202"),
            ("test_ready_answers_with_a_status", "ConnectionRefusedError: [Errno 111] Connection refused"),
        ),
        seconds=140,
    )

    code, said = summary(tmp_path, "--require", "integration", capsys=capsys)

    assert code == 1
    assert re.search(r"^### ❌ Nightly, \d{4}-\d\d-\d\d — 2 failed of 59$", said, flags=re.M)
    assert "| integration | 57 | 2 | 0 | 2.3 min |" in said
    assert "- `tests.integration.test_x::test_plan_then_refine_end_to_end` (integration) — assert 500 == 202" in said
    assert "ConnectionRefusedError: [Errno 111] Connection refused" in said
    assert "trace" not in said  # the first line of why, not the traceback


def test_a_suite_that_left_no_report_did_not_pass(tmp_path, capsys):
    """The tests crashed before the first one ran — an import error, a database that never came up. There is no
    report, so there are no failures in it; read naively, that is a perfect night."""
    code, said = summary(
        tmp_path / "nothing-here", "--require", "integration", "--absent", "evals=later", capsys=capsys
    )

    assert code == 1
    assert "— 1 failed" in said and "| integration | — | — | — | DID NOT RUN — no report was written |" in said

    junit(tmp_path, "evals", passed=3)  # the other suite ran: still not a good night without the one that had to
    code, said = summary(tmp_path, "--require", "integration", capsys=capsys)
    assert code == 1 and "| evals | 3 | 0 | 0 |" in said and "DID NOT RUN" in said

    code, said = summary(tmp_path / "nothing-here", "--absent", "evals=later", capsys=capsys)
    assert code == 1 and "nothing ran" in said  # and a night in which nothing ran at all is not one either


def test_a_report_that_cannot_be_read_is_a_suite_that_did_not_finish(tmp_path, capsys):
    (tmp_path / "integration.xml").write_text('<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="60"')
    junit(tmp_path, "evals", passed=3)

    code, said = summary(tmp_path, "--require", "integration", capsys=capsys)

    assert code == 1  # not a traceback and an empty comment: a night that says what happened
    assert "| integration | — | — | — | DID NOT FINISH — its report cannot be read |" in said
    assert "| evals | 3 | 0 | 0 |" in said and "— 1 failed of 3" in said


def test_a_long_list_of_failures_is_cut_short_and_says_so(tmp_path, capsys):
    junit(tmp_path, "integration", failed=tuple((f"test_{n}", "boom") for n in range(40)))

    code, said = summary(tmp_path, capsys=capsys)

    assert code == 1 and said.count("\n- `") == 15 and "- …and 25 more — see the run" in said


def test_the_summary_reads_what_pytest_really_writes(tmp_path, capsys):
    (tmp_path / "test_tiny.py").write_text(
        "import pytest\n\n"
        "@pytest.fixture\ndef database():\n    raise ConnectionRefusedError('the database never came up')\n\n"
        "def test_passes():\n    assert True\n\n"
        "def test_fails():\n    assert 1 + 1 == 3, 'arithmetic is not what it was'\n\n"
        "def test_never_begins(database):\n    assert True\n"
    )
    reports = tmp_path / "reports"
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_tiny.py",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={reports}/integration.xml",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert ran.returncode == 1, ran.stdout

    code, said = summary(reports, "--require", "integration", capsys=capsys)

    # a test that could not begin (pytest calls it an error, not a failure) did not pass either
    assert code == 1 and "| integration | 1 | 2 | 0 |" in said and "— 2 failed of 3" in said
    assert "`test_tiny::test_fails` (integration) — AssertionError: arithmetic is not what it was" in said
    assert re.search(r"`test_tiny::test_never_begins` \(integration\) — .*ConnectionRefusedError", said), said


# ── Posting it ─────────────────────────────────────────────────────────────


@pytest.fixture
def github(tmp_path):
    """Run scripts/nightly_post.sh against a stand-in for GitHub's CLI. → post(summary text) -> (exit code, said)"""
    state = tmp_path / "github"
    state.mkdir()
    (state / "summary.md").write_text("")

    def post(text: str) -> tuple[int, str]:
        (state / "summary.md").write_text(text)
        done = subprocess.run(
            ["bash", "scripts/nightly_post.sh", str(state / "summary.md")],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], "GH_BIN": str(ROOT / "tests/ci/fake_gh.sh"), "FAKE_GH_DIR": str(state)},
            capture_output=True,
            text=True,
        )
        return done.returncode, done.stdout + done.stderr

    post.state = state
    return post


def test_the_first_night_opens_the_issue_and_every_night_after_adds_a_comment_to_it(github):
    code, said = github("### ✅ Nightly, 2026-10-12 — 60 passed\n")
    assert code == 0 and "made the nightly issue: #7" in said and "posted the night's summary to issue #7" in said
    calls = (github.state / "calls.log").read_text().splitlines()
    assert calls[0].startswith("label create nightly")
    assert calls[1] == "issue list --label nightly --state open --limit 1 --json number --jq .[0].number // empty"
    assert calls[2] == "issue create --title Nightly test results --label nightly --body-file .github/nightly-issue.md"
    assert calls[3].startswith("issue comment 7 --body-file ")
    assert (github.state / "issue-body.md").read_text() == (ROOT / ".github/nightly-issue.md").read_text()

    code, said = github("### ❌ Nightly, 2026-10-13 — 2 failed of 60\n")  # the label is there now: that is not an error
    assert code == 0 and "made the nightly issue" not in said
    later = (github.state / "calls.log").read_text().splitlines()[4:]
    assert [call.split()[0:2] for call in later] == [["label", "create"], ["issue", "list"], ["issue", "comment"]]
    comments = (github.state / "comment-7.md").read_text()
    assert comments.index("2026-10-12 — 60 passed") < comments.index("2026-10-13 — 2 failed")  # one issue, a history


def test_a_night_with_nothing_to_say_posts_nothing_and_says_that_it_did_not(github):
    code, said = github("")

    assert code == 1 and "missing or empty — nothing to post" in said
    assert not (github.state / "calls.log").exists()  # GitHub was not asked anything
