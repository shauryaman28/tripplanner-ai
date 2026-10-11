"""The night's test results, as the one comment that is posted about them (Phase 28).

    python scripts/nightly_summary.py reports/ --require integration \\
        --absent evals="no eval suite yet (Phase 33)" --commit <sha> --run-url <url> > summary.md

Reads every JUnit report in a directory (`pytest --junitxml=reports/<suite>.xml`; the file's name is
the suite's) and writes Markdown to standard output:

    ### ✅ Nightly, 2026-10-12 — 60 passed

    | Suite | Passed | Failed | Skipped | Took |
    |---|---|---|---|---|
    | integration | 60 | 0 | 0 | 31 s |
    | evals | — | — | — | not run: no eval suite yet (Phase 33) |

    Commit `3f2a9c1` · [the run](https://github.com/…/actions/runs/1)

and, when something failed, which tests and the first line of why.

The exit code is the night's verdict: 0 when every suite that ran passed and every suite that had to
run did. A report that is missing is not "no failures" — a suite named with `--require` that left
no report did not run (the tests crashed before they began, or the step was never reached), and that
is a failed night; so is a report that cannot be read (a run killed half-way through writing it).
`--absent name=reason` is for a suite that is known not to exist yet.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_FAILURES_SHOWN = 15


@dataclass
class Suite:
    name: str
    tests: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (test, the first line of why)
    not_run: str | None = None  # why there is nothing to count

    @property
    def passed(self) -> int:
        return self.tests - self.failed - self.skipped

    @property
    def ok(self) -> bool:
        return self.failed == 0


def read_report(path: Path) -> Suite:
    """One JUnit file → one suite. pytest writes <testsuites><testsuite …>; some tools write the suite alone."""
    suite = Suite(name=path.stem)
    root = ET.parse(path).getroot()
    for node in [root] if root.tag == "testsuite" else root.iter("testsuite"):
        suite.tests += int(node.get("tests") or 0)
        suite.failed += int(node.get("failures") or 0) + int(node.get("errors") or 0)
        suite.skipped += int(node.get("skipped") or 0)
        suite.seconds += float(node.get("time") or 0.0)
        for case in node.iter("testcase"):
            for problem in list(case.iter("failure")) + list(case.iter("error")):
                where = "::".join(part for part in (case.get("classname"), case.get("name")) if part)
                why = (problem.get("message") or problem.text or "").strip().splitlines()
                suite.failures.append((where, why[0][:200] if why else ""))
    return suite


def gather(directory: Path, require: list[str], absent: dict[str, str]) -> list[Suite]:
    """Every report there is, then what had to be there and is not, then what is known not to exist yet."""
    found: dict[str, Suite] = {}
    for path in sorted(directory.glob("*.xml")) if directory.is_dir() else []:
        try:
            found[path.stem] = read_report(path)
        except ET.ParseError as exc:
            broken = Suite(name=path.stem, failed=1, not_run="DID NOT FINISH — its report cannot be read")
            broken.failures.append((path.stem, f"{path.name}: {exc}"))
            found[path.stem] = broken
    suites = list(found.values())
    for name in require:
        if name not in found:
            missing = Suite(name=name, failed=1, not_run="DID NOT RUN — no report was written")
            missing.failures.append(
                (name, "the suite left no report: it crashed before its first test, or was never started")
            )
            suites.append(missing)
    for name, reason in absent.items():
        if name not in found and name not in require:
            suites.append(Suite(name=name, not_run=f"not run: {reason}"))
    return suites


def took(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def render(suites: list[Suite], commit: str = "", run_url: str = "", today: str | None = None) -> str:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failed = sum(suite.failed for suite in suites)
    passed = sum(suite.passed for suite in suites if suite.not_run is None)
    ran = sum(suite.tests for suite in suites if suite.not_run is None)
    if failed:
        verdict = f"❌ Nightly, {today} — {failed} failed" + (f" of {ran}" if ran else "")
    elif ran == 0:
        verdict = f"❌ Nightly, {today} — nothing ran"
    else:
        verdict = f"✅ Nightly, {today} — {passed} passed"

    lines = [f"### {verdict}", "", "| Suite | Passed | Failed | Skipped | Took |", "|---|---|---|---|---|"]
    for suite in suites:
        if suite.not_run is not None:
            lines.append(f"| {suite.name} | — | — | — | {suite.not_run} |")
        else:
            lines.append(
                f"| {suite.name} | {suite.passed} | {suite.failed} | {suite.skipped} | {took(suite.seconds)} |"
            )

    failures = [(suite.name, test, why) for suite in suites for test, why in suite.failures if suite.not_run is None]
    if failures:
        lines += ["", "**What failed:**", ""]
        for suite_name, test, why in failures[:MAX_FAILURES_SHOWN]:
            lines.append(f"- `{test}` ({suite_name})" + (f" — {why}" if why else ""))
        if len(failures) > MAX_FAILURES_SHOWN:
            lines.append(f"- …and {len(failures) - MAX_FAILURES_SHOWN} more — see the run")

    about = [
        part
        for part in (f"Commit `{commit[:7]}`" if commit else "", f"[the run]({run_url})" if run_url else "")
        if part
    ]
    if about:
        lines += ["", " · ".join(about)]
    return "\n".join(lines) + "\n"


def verdict(suites: list[Suite]) -> bool:
    """A good night: something ran, and nothing that ran — or had to — failed."""
    return all(suite.ok for suite in suites) and any(suite.not_run is None and suite.tests for suite in suites)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the night's test results as one Markdown comment.")
    parser.add_argument("directory", type=Path, help="where the JUnit reports are")
    parser.add_argument(
        "--require", action="append", default=[], help="a suite whose report must be there (repeatable)"
    )
    parser.add_argument(
        "--absent", action="append", default=[], metavar="NAME=REASON", help="a suite known not to exist yet"
    )
    parser.add_argument("--commit", default="")
    parser.add_argument("--run-url", default="")
    args = parser.parse_args(argv)

    absent = dict(item.split("=", 1) if "=" in item else (item, "not built yet") for item in args.absent)
    suites = gather(args.directory, args.require, absent)
    sys.stdout.write(render(suites, args.commit, args.run_url))
    return 0 if verdict(suites) else 1


if __name__ == "__main__":
    sys.exit(main())
