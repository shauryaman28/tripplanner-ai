"""The eval suite (Phase 33): fifteen trips asked of the planner, graded, and the verdict.

    python scripts/run_eval.py                        # this checkout's backend, started against a database of its own
    python scripts/run_eval.py --assert-min-score 3.5 # …and exit 1 if the average falls below 3.5 (the nightly run)
    python scripts/run_eval.py --api http://localhost:8200    # a backend that is already running (the production-like stack)
    python scripts/run_eval.py --only happy-jaipur edge-past-date
    python scripts/run_eval.py --plan-only            # ask the planner and keep what came back; grade later
    python scripts/run_eval.py --regrade .evals/<run> # grade what an earlier run kept, without asking the planner again

Writes `docs/eval-results-<date>[-<label>].md` (the table), appends a line to `docs/eval-history.csv`, and
keeps each scenario's evidence in `.evals/<run>/` (git-ignored; `--evidence DIR` to keep it elsewhere).

Needs: Postgres and Redis (the dev containers), the provider keys in `.env` — the plans are real — and
ANTHROPIC_API_KEY for the grader, Claude Haiku 4.5. On Groq's free tier the run is paced to its
per-minute token quota (`--gap`, 45 s between plans); on a paid tier, `--gap 0`.

Exit: 0 passed · 1 the average is below --assert-min-score · 2 something could not be run or graded.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src" / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evals import driver, grader, measure, report  # noqa: E402
from evals.scenarios import BY_ID, Scenario, pick  # noqa: E402

DOCS = ROOT / "docs"
HISTORY = DOCS / "eval-history.csv"
EVIDENCE = ROOT / ".evals"
# What a plan cannot be made without: the builder's model, flights, hotels, attractions. (Gemini has Groq to
# fall back on; the weather is only warmed.) The backend reads them from .env or the environment.
NEEDED_FOR_PLANS = ("GROQ_API_KEY", "DUFFEL_ACCESS_TOKEN", "LITEAPI_API_KEY", "OPENTRIPMAP_API_KEY")
RETRY_AFTER_REFUSAL_S = 60.0  # a provider that refused for its per-minute quota has forgotten it a minute later
GRADERS_AT_ONCE = 4


def missing_keys(names: tuple[str, ...]) -> list[str]:
    from dotenv import dotenv_values

    in_file = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    return [name for name in names if not (os.environ.get(name) or in_file.get(name) or "").strip()]


def not_configured(why: str, suite: report.Suite, junit: Path | None) -> int:
    """Nothing can be asked or graded: say so — and, for the nightly summary, a suite that was skipped, not failed."""
    print(f"Not run: {why}", file=sys.stderr)
    if junit:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(report.junit(suite, not_configured=why))
    return report.INCOMPLETE


def commit() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    sha = git("rev-parse", "HEAD")
    return f"{sha}+changes" if sha and git("status", "--porcelain", "--untracked-files=no") else sha


def save(evidence: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{evidence['scenario']}.json"
    kept = {k: v for k, v in evidence.items() if not k.startswith("_")}  # the sign-in token is never written
    path.write_text(json.dumps(kept, ensure_ascii=False, indent=1, default=str))
    return path


def load(folder: Path) -> list[dict]:
    found = {p.stem: json.loads(p.read_text()) for p in folder.glob("*.json") if p.stem in BY_ID}
    if not found:
        raise SystemExit(f"no scenario records in {folder}")
    return [found[s.id] for s in pick(list(found))]


# ── Asking ──────────────────────────────────────────────────────────────────


def kept_awake(seconds: float) -> subprocess.Popen | None:
    """On a Mac, ask it not to sleep while the scenarios are asked (caffeinate). With the lid open that holds;
    with it closed nothing a program does prevents sleep — which the evidence then records (asleep_s)."""
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-ims", "-t", str(int(seconds))])
    except OSError:
        return None


def ask_all(url: str, scenarios: list[Scenario], gap_s: float, folder: Path) -> tuple[list[dict], float]:
    awake = kept_awake(60 * 60)
    try:
        return _ask_all(url, scenarios, gap_s, folder)
    finally:
        if awake:
            awake.terminate()


def _ask_all(url: str, scenarios: list[Scenario], gap_s: float, folder: Path) -> tuple[list[dict], float]:
    pacer = driver.Pacer(gap_s)
    done: dict[str, dict] = {}
    for number, scenario in enumerate(scenarios, 1):
        base = done.get(scenario.refines) if scenario.refines else None
        evidence = driver.run(driver.Api(url), scenario, pacer, base)
        if evidence.get("provider_refused") and evidence.get("outcome") != "plan":
            print(
                f"    {scenario.id}: a model provider refused for quota — asking again in {RETRY_AFTER_REFUSAL_S:.0f} s"
            )
            time.sleep(RETRY_AFTER_REFUSAL_S)
            pacer.waited_s += RETRY_AFTER_REFUSAL_S
            evidence = driver.run(driver.Api(url), scenario, pacer, base)
            evidence["asked_again_after_refusal"] = True
        done[scenario.id] = evidence
        save(evidence, folder)
        right, said = measure.outcome(scenario, evidence)
        mark = {True: "✓", False: "✗", None: "–"}[right]
        slept = f", asleep {evidence['asleep_s']:.0f} s of it" if (evidence.get("asleep_s") or 0) >= 1 else ""
        print(
            f"  {mark} [{number:>2}/{len(scenarios)}] {scenario.id:<22} {said}  ({evidence['seconds']:.0f} s{slept})",
            flush=True,
        )
    return [done[s.id] for s in scenarios], pacer.waited_s


# ── Grading ─────────────────────────────────────────────────────────────────


def grade_all(evidences: list[dict], client, model: str) -> list[report.ScenarioResult]:
    def one(evidence: dict) -> report.ScenarioResult:
        scenario = BY_ID[evidence["scenario"]]
        seconds = evidence.get("seconds") or 0.0
        right, said = measure.outcome(scenario, evidence)
        if right is not True:
            return report.combine(scenario, right, said, {}, None, seconds)
        try:
            measures = measure.measure(scenario, evidence)
            graded = grader.grade(client, scenario, evidence, said, measures, model) if measures else None
        except Exception as exc:  # the grader, or a record the measurements cannot read: not the build's score
            return report.not_graded(scenario, f"{type(exc).__name__}: {exc}", seconds)
        return report.combine(scenario, right, said, measures, graded, seconds)

    with ThreadPoolExecutor(GRADERS_AT_ONCE) as pool:
        return list(pool.map(one, evidences))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--api", help="a backend that is already running; by default this checkout's is started")
    where.add_argument("--regrade", type=Path, metavar="DIR", help="grade the evidence an earlier run kept in DIR")
    parser.add_argument("--assert-min-score", type=float, metavar="SCORE", help="exit 1 if the average is below it")
    parser.add_argument("--only", nargs="+", metavar="ID", help="these scenarios (and the plans a refinement needs)")
    parser.add_argument("--label", default="", help="named in the results file and the history: e.g. broken-builder")
    parser.add_argument("--gap", type=float, default=driver.DEFAULT_GAP_S, help="seconds between plans (the quota)")
    parser.add_argument("--evidence", type=Path, metavar="DIR", help="where to keep what came back")
    parser.add_argument("--plan-only", action="store_true", help="ask, keep the evidence, and grade nothing")
    parser.add_argument("--no-history", action="store_true", help="write no line in docs/eval-history.csv")
    parser.add_argument("--junit", type=Path, metavar="FILE", help="also a JUnit report (for the nightly summary)")
    parser.add_argument("--results", type=Path, default=DOCS, metavar="DIR", help="where eval-results-<date>.md goes")
    parser.add_argument("--model", default=grader.MODEL, help="the grader's model")
    args = parser.parse_args(argv)

    began = time.monotonic()
    run_at = datetime.now(timezone.utc).replace(microsecond=0)
    label = args.label or ("regrade" if args.regrade else "")
    suite = report.Suite(
        [], args.assert_min_score, label=label, commit=commit(), run_at=run_at.isoformat().replace("+00:00", "")
    )

    client = None
    if not args.plan_only:
        try:
            client = grader.client_from_env()
        except grader.GraderError as exc:
            return not_configured(str(exc), suite, args.junit)
    if not args.regrade and not args.api and (missing := missing_keys(NEEDED_FOR_PLANS)):
        return not_configured(
            f"the plans are real, and these keys are not set: {', '.join(missing)}", suite, args.junit
        )

    if args.regrade:
        evidences = load(args.regrade)
        if args.only:
            evidences = [e for e in evidences if e["scenario"] in args.only]
        suite.against, waited = f"the evidence in {args.regrade}", 0.0
        planning_s = sum(e.get("seconds") or 0.0 for e in evidences)
    else:
        scenarios = pick(args.only)
        folder = args.evidence or EVIDENCE / (run_at.strftime("%Y-%m-%dT%H%M%S") + (f"-{label}" if label else ""))
        print(
            f"{len(scenarios)} scenarios → {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}  (gap between plans {args.gap:.0f} s)"
        )
        try:
            if args.api:
                if not driver.answers(args.api):
                    print(f"Nothing answers at {args.api}/ping", file=sys.stderr)
                    return report.INCOMPLETE
                suite.against = args.api
                evidences, waited = ask_all(args.api, scenarios, args.gap, folder)
            else:
                with driver.serve(folder / "backend.log") as url:
                    suite.against = f"this checkout, served at {url} against its own database"
                    evidences, waited = ask_all(url, scenarios, args.gap, folder)
        except driver.HarnessError as exc:
            print(f"Could not run the scenarios: {exc}", file=sys.stderr)
            return report.INCOMPLETE
        planning_s = time.monotonic() - began
        if args.plan_only:
            print(f"Kept in {folder}; grade it with: python scripts/run_eval.py --regrade {folder}")
            return report.PASSED

    print(f"Grading {len(evidences)} with {args.model}…", flush=True)
    graded_at = time.monotonic()
    suite.results = grade_all(evidences, client, args.model)
    suite.seconds = planning_s + (time.monotonic() - graded_at)
    suite.waited_s = waited
    suite.asleep_s = sum(e.get("asleep_s") or 0.0 for e in evidences)

    args.results.mkdir(parents=True, exist_ok=True)
    name = f"eval-results-{run_at.date().isoformat()}" + (f"-{label}" if label else "") + ".md"
    (args.results / name).write_text(report.markdown(suite))
    if not args.no_history:
        report.append_history(HISTORY, suite)
    if args.junit:
        args.junit.parent.mkdir(parents=True, exist_ok=True)
        args.junit.write_text(report.junit(suite))

    print()
    for result in suite.results:
        score = "–" if result.score is None else result.score
        print(f"  {score!s:>2}  {result.scenario.id:<22} {'; '.join(result.why)[:150]}")
    print(f"\n{suite.verdict_words()} · {report._minutes(suite.seconds)} · grader ${suite.grader_cost:.4f}")
    print(
        f"→ {(args.results / name).relative_to(ROOT) if (args.results / name).is_relative_to(ROOT) else args.results / name}"
    )
    return suite.verdict


if __name__ == "__main__":
    sys.exit(main())
