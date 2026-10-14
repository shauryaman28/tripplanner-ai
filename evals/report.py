"""From measurements and grades to a score, a table, a line of history and a verdict (Phase 33).

A criterion's score is the grader's, held to the ceiling the measurements set. A scenario's score is
its lowest criterion's — a plan is as good as its worst fault: a plan that invents a hotel is not a
four because its days are full. (An average of the criteria would let one criterion fail everywhere and
the suite still pass: four criteria at 5, 5, 5 and 1 average 4.) A scenario that did not end as it
should scores 0; one that could not be run is not scored, and is listed.

The suite's score is the average over the scenarios scored. `--assert-min-score` is checked against it.

    exit 0   every scenario run and graded, and the average at or above the threshold
    exit 1   the average below the threshold: the build got worse
    exit 2   some scenario could not be run or graded — the average does not vouch for the build
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from evals.grader import Graded
from evals.measure import Measure
from evals.scenarios import BUDGET, COMPLETENESS, CRITERIA, HALLUCINATION, REFINEMENT, TOOLS, Scenario

PASSED, BELOW, INCOMPLETE = 0, 1, 2

SHORT = {TOOLS: "Tools", BUDGET: "Budget", HALLUCINATION: "Halluc.", COMPLETENESS: "Complete", REFINEMENT: "Refine"}
LABEL = {
    TOOLS: "tool correctness",
    BUDGET: "budget consistency",
    HALLUCINATION: "hallucination",
    COMPLETENESS: "completeness",
    REFINEMENT: "refinement accuracy",
}
HISTORY_FIELDS = (
    "run_at", "commit", "label", "average", "threshold", "verdict", "scenarios", "scored", "not_run",
    *CRITERIA, "minutes", "grader", "grader_cost_usd",
)  # fmt: skip


@dataclass
class CriterionResult:
    criterion: str
    score: int
    grader: int
    ceiling: int
    reasons: list[str]
    measured: list[str]


@dataclass
class ScenarioResult:
    scenario: Scenario
    right: bool | None  # None: not run
    said: str
    seconds: float = 0.0
    criteria: dict[str, CriterionResult] = field(default_factory=dict)
    summary: str = ""
    graded: Graded | None = None

    @property
    def score(self) -> int | None:
        if self.right is None:
            return None
        if self.right is False:
            return 0
        return min((c.score for c in self.criteria.values()), default=5)

    @property
    def why(self) -> list[str]:
        if self.right is None:
            return [self.said]
        if self.right is False:
            return [f"expected {self.scenario.expect}: {self.said}"]
        lines = []
        for c in self.criteria.values():
            if c.score >= 5:
                continue
            capped = (
                f" (the grader gave {c.grader}; held to {c.ceiling} by: {'; '.join(c.measured)})"
                if c.ceiling < c.grader
                else ""
            )
            lines.append(
                f"{LABEL[c.criterion]} {c.score}: {'; '.join(c.reasons[:2]) or '; '.join(c.measured[:2])}{capped}"
            )
        return lines


def combine(
    scenario: Scenario,
    right: bool | None,
    said: str,
    measures: dict[str, Measure],
    graded: Graded | None,
    seconds: float = 0.0,
) -> ScenarioResult:
    result = ScenarioResult(scenario, right, said, seconds=seconds, graded=graded)
    if right is not True or not measures:
        return result
    if graded is None:
        raise ValueError(f"{scenario.id}: measured, and not graded")
    for criterion, measure in measures.items():
        given = graded.scores[criterion]
        result.criteria[criterion] = CriterionResult(
            criterion=criterion,
            score=min(given, measure.ceiling),
            grader=given,
            ceiling=measure.ceiling,
            reasons=graded.reasons.get(criterion, []),
            measured=[problem for _, problem in measure.problems],
        )
    result.summary = graded.summary
    return result


def not_graded(scenario: Scenario, why: str, seconds: float = 0.0) -> ScenarioResult:
    return ScenarioResult(scenario, None, f"not graded: {why}", seconds=seconds)


# ── The suite ───────────────────────────────────────────────────────────────


@dataclass
class Suite:
    results: list[ScenarioResult]
    threshold: float | None
    label: str = ""
    commit: str = ""
    run_at: str = ""  # UTC, ISO 8601
    seconds: float = 0.0
    waited_s: float = 0.0
    asleep_s: float = 0.0  # how long the machine slept while the scenarios were asked: then the time says nothing
    against: str = ""

    @property
    def scored(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.score is not None]

    @property
    def not_run(self) -> list[ScenarioResult]:
        return [r for r in self.results if r.score is None]

    @property
    def average(self) -> float | None:
        scored = self.scored
        return round(sum(r.score for r in scored) / len(scored), 2) if scored else None

    def criterion_average(self, criterion: str) -> float | None:
        scores = [r.criteria[criterion].score for r in self.scored if criterion in r.criteria]
        return round(sum(scores) / len(scores), 2) if scores else None

    @property
    def grader_tokens(self) -> tuple[int, int]:
        graded = [r.graded for r in self.results if r.graded]
        return sum(g.input_tokens for g in graded), sum(g.output_tokens for g in graded)

    @property
    def grader_cost(self) -> float:
        return round(sum(r.graded.cost_usd for r in self.results if r.graded), 4)

    @property
    def grader_model(self) -> str:
        return next((r.graded.model for r in self.results if r.graded), "")

    @property
    def verdict(self) -> int:
        if self.not_run or self.average is None:
            return INCOMPLETE
        if self.threshold is not None and self.average < self.threshold:
            return BELOW
        return PASSED

    def verdict_words(self) -> str:
        average = "—" if self.average is None else f"{self.average:.2f}"
        if self.verdict == INCOMPLETE:
            return f"incomplete — {len(self.not_run)} of {len(self.results)} not scored"
        if self.threshold is None:
            return f"average {average} (no threshold given)"
        return f"average {average} {'≥' if self.verdict == PASSED else '<'} {self.threshold} — {'passed' if self.verdict == PASSED else 'BELOW THE THRESHOLD'}"


# ── The table ───────────────────────────────────────────────────────────────


def _cell(result: ScenarioResult, criterion: str) -> str:
    if criterion not in result.criteria:
        return "·"
    c = result.criteria[criterion]
    return f"{c.score}" + ("↓" if c.ceiling < c.grader else "")


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"


def markdown(suite: Suite) -> str:
    title = f"# Eval results — {suite.run_at[:10]}" + (f" ({suite.label})" if suite.label else "")
    tokens_in, tokens_out = suite.grader_tokens
    lines = [
        title,
        "",
        f"**{suite.verdict_words()}** · {len(suite.scored)} of {len(suite.results)} scenarios scored",
        "",
        f"- Took {_minutes(suite.seconds)}, of which {_minutes(suite.waited_s)} waiting between plans for the model provider's per-minute token quota"
        + (
            f" — **this time does not hold: the machine slept {_minutes(suite.asleep_s)} of it**"
            if suite.asleep_s >= 1
            else ""
        ),
        f"- Against: {suite.against or '—'} · commit `{suite.commit[:7] or '—'}` · run at {suite.run_at} UTC",
        f"- Grader: {suite.grader_model or '—'} — {tokens_in:,} tokens in, {tokens_out:,} out, ${suite.grader_cost:.4f}",
        "",
        "| # | Scenario | Kind | " + " | ".join(SHORT[c] for c in CRITERIA) + " | **Score** | Why |",
        "|---|---|---|" + "---|" * len(CRITERIA) + "---|---|",
    ]
    for number, result in enumerate(suite.results, 1):
        score = "not run" if result.score is None else f"**{result.score}**"
        why = "<br>".join(_escape(line) for line in result.why) or "—"
        lines.append(
            f"| {number} | `{result.scenario.id}` | {result.scenario.kind} | "
            + " | ".join(_cell(result, c) for c in CRITERIA)
            + f" | {score} | {why} |"
        )
    lines += [
        "",
        "`·` not graded for this scenario · `↓` the grader's score held down by what was measured (see below)",
        "",
        "## By criterion",
        "",
        "| Criterion | Average | Scenarios graded on it |",
        "|---|---|---|",
    ]
    for criterion in CRITERIA:
        average = suite.criterion_average(criterion)
        count = sum(1 for r in suite.scored if criterion in r.criteria)
        lines.append(f"| {LABEL[criterion]} | {'—' if average is None else f'{average:.2f}'} | {count} |")
    lines += ["", "## Each scenario", ""]
    for result in suite.results:
        s = result.scenario
        lines.append(f"### `{s.id}` — {'not run' if result.score is None else f'{result.score} / 5'}")
        lines.append("")
        lines.append(f"{s.asks}. Expected: {s.expect}. What happened: {result.said} ({result.seconds:.0f} s).")
        if result.summary:
            lines.append(f"\n> {_escape(result.summary)}")
        for c in result.criteria.values():
            lines.append(f"\n- **{LABEL[c.criterion]} {c.score}** (grader {c.grader}, measured ceiling {c.ceiling})")
            lines += [f"  - {_escape(reason)}" for reason in c.reasons]
            lines += [f"  - measured: {_escape(problem)}" for problem in c.measured]
        lines.append("")
    lines += [
        "## How it was graded",
        "",
        "Each scenario was asked of the backend over its API, as a traveller would, and everything it served kept "
        "(the trip, its status, its itineraries, every agent run). Code measured what can be measured exactly — the "
        "searches' parameters, `total_cost` against `estimate_budget`, every name against the search results, the "
        "dates and slots, what a change changed — and Claude Haiku 4.5 read the rest, given those measurements as "
        "facts. A criterion scores what the grader gave, no higher than the measurements allow; a scenario scores "
        "its lowest criterion, and 0 if it did not end as it should. See `evals/` and `docs/phase33_build_log.md`.",
        "",
    ]
    return "\n".join(lines)


def _escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


# ── The history ─────────────────────────────────────────────────────────────


def history_row(suite: Suite) -> dict:
    row = {
        "run_at": suite.run_at,
        "commit": suite.commit[:12],
        "label": suite.label,
        "average": "" if suite.average is None else f"{suite.average:.2f}",
        "threshold": "" if suite.threshold is None else suite.threshold,
        "verdict": {PASSED: "passed", BELOW: "below", INCOMPLETE: "incomplete"}[suite.verdict],
        "scenarios": len(suite.results),
        "scored": len(suite.scored),
        "not_run": ";".join(r.scenario.id for r in suite.not_run),
        "minutes": f"{suite.seconds / 60:.1f}" if suite.asleep_s < 1 else "",  # a time slept through is no time
        "grader": suite.grader_model,
        "grader_cost_usd": f"{suite.grader_cost:.4f}",
    }
    for criterion in CRITERIA:
        average = suite.criterion_average(criterion)
        row[criterion] = "" if average is None else f"{average:.2f}"
    return row


def append_history(path: Path, suite: Suite) -> None:
    """One line per run, appended; the header written once. Rows already there are never rewritten."""
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="") as existing:
            header = next(csv.reader(existing), [])
        if tuple(header) != HISTORY_FIELDS:
            raise ValueError(f"{path} has columns {header}, not {list(HISTORY_FIELDS)}: not appending to it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=HISTORY_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(history_row(suite))


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return list(csv.DictReader(io.StringIO(path.read_text())))


# ── For the nightly run ─────────────────────────────────────────────────────


def junit(suite: Suite, not_configured: str | None = None) -> str:
    """A JUnit report the nightly summary reads (scripts/nightly_summary.py): one case for each scenario,
    one for the threshold, one for "every scenario was scored". Not configured (no keys): all skipped."""
    cases: list[tuple[str, str | None, str | None]] = []  # (name, failure, skipped)
    if not_configured:
        cases.append((f"eval suite: {not_configured}", None, not_configured))
    else:
        for result in suite.results:
            if result.score is None:
                cases.append((result.scenario.id, None, result.said))
            else:
                cases.append((f"{result.scenario.id}: {result.score}/5", None, None))
        if suite.threshold is not None:
            ok = suite.average is not None and suite.average >= suite.threshold
            cases.append(
                (
                    f"average score ≥ {suite.threshold}",
                    None
                    if ok
                    else f"average {suite.average} < {suite.threshold}: "
                    + "; ".join(
                        f"{r.scenario.id} {r.score}" for r in suite.scored if r.score is not None and r.score < 4
                    ),
                    None,
                )
            )
        if suite.not_run:
            cases.append(
                (
                    "every scenario run and graded",
                    f"not scored: {', '.join(r.scenario.id for r in suite.not_run)}",
                    None,
                )
            )
        else:
            cases.append(("every scenario run and graded", None, None))
    root = ET.Element(
        "testsuite",
        name="evals",
        tests=str(len(cases)),
        failures=str(sum(1 for _, failed, _ in cases if failed)),
        skipped=str(sum(1 for _, _, skipped in cases if skipped)),
        time=f"{suite.seconds:.1f}",
    )
    for name, failed, skipped in cases:
        case = ET.SubElement(root, "testcase", classname="evals", name=name)
        if failed:
            ET.SubElement(case, "failure", message=failed.splitlines()[0][:300]).text = failed
        if skipped:
            ET.SubElement(case, "skipped", message=skipped[:300])
    return ET.tostring(root, encoding="unicode")
