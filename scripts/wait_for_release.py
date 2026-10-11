"""Wait until a deploy is live — until what answers at the public addresses is the commit that was merged.

    python scripts/wait_for_release.py --commit <sha> --api https://<backend> --web https://<frontend>
    python scripts/wait_for_release.py --commit <sha> --api … --web … --since 2026-10-11T12:00:00Z --timeout 600

"The deploy succeeded" is what a platform says when it has started a container, or queued a build.
This asks the thing itself, the way a traveller's browser would reach it:

    <backend>/version        the backend says which commit it is running
    <frontend>/version       the site says which commit it was built from
    <frontend>/api/version   …and the site's own /api/* reaches a backend that is running it too

and returns when all of them answer with `--commit`. Each address given is asked every few seconds;
one that is not given is not asked (a backend can be waited for on its own).

`--since` is when the merge was pushed. With it, the time from merge to live is said, and compared
with `--budget` (300 s — the roadmap's "within 5 minutes"): slower than that is a warning, not a
failure — a deploy that is live is live. Not live at all within `--timeout` is exit code 1, with
what each address last answered.

Used by .github/workflows/deploy.yml, and by hand after any deploy. No dependency beyond Python.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

INTERVAL_S = 5.0


def fetch_json(url: str, timeout: float = 10.0) -> dict:
    """GET a JSON document. httpx when it is there (a developer's environment), else the standard library."""
    try:
        import httpx
    except ImportError:
        import urllib.request

        request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 — an address the caller gave
            return json.loads(answer.read().decode())
    answer = httpx.get(url, timeout=timeout, headers={"Cache-Control": "no-cache"}, follow_redirects=True)
    answer.raise_for_status()
    return answer.json()


def same_commit(deployed: object, wanted: str) -> bool:
    """A short SHA and a long one are the same commit when one begins with the other (seven characters at least)."""
    if not isinstance(deployed, str) or len(deployed) < 7 or len(wanted) < 7:
        return False
    return deployed.startswith(wanted) or wanted.startswith(deployed)


def seconds_since(moment: str | None, now: float | None = None) -> float | None:
    """How long ago an ISO timestamp was — None when there is none, or it cannot be read."""
    if not moment:
        return None
    try:
        then = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now if now is not None else time.time()) - then.timestamp()


def addresses(api: str | None, web: str | None) -> dict[str, str]:
    """What is asked: name → URL."""
    asked = {}
    if api:
        asked["the backend"] = f"{api.rstrip('/')}/version"
    if web:
        asked["the site"] = f"{web.rstrip('/')}/version"
        asked["the site's /api"] = f"{web.rstrip('/')}/api/version"
    return asked


def wait(asked: dict[str, str], commit: str, timeout: float, fetch=None, sleep=None, clock=None):
    """Ask every address until each has answered with the commit. → (live: {name: seconds}, last: {name: answer}).

    `fetch`, `sleep` and `clock` are this module's own unless others are handed in (the tests hand in a
    clock that only moves when something sleeps).
    """
    fetch, sleep, clock = fetch or fetch_json, sleep or time.sleep, clock or time.monotonic
    began = clock()
    live: dict[str, float] = {}
    last: dict[str, str] = {name: "nothing yet" for name in asked}
    while True:
        for name, url in asked.items():
            if name in live:
                continue
            try:
                answer = fetch(url)
                running = answer.get("commit") if isinstance(answer, dict) else None
                last[name] = f"commit {str(running)[:12]}" if running else "no commit in its answer"
                if same_commit(running, commit):
                    live[name] = clock() - began
                    print(f"  ✓ {name} is running {commit[:7]}  (after {live[name]:.0f} s)", flush=True)
            except Exception as exc:
                last[name] = f"{type(exc).__name__}: {str(exc)[:80]}"
        if len(live) == len(asked) or clock() - began >= timeout:
            return live, last
        sleep(INTERVAL_S)


def annotate(kind: str, text: str) -> None:
    """A line GitHub shows on the run's page; plain text anywhere else."""
    print(f"::{kind}::{text}" if os.environ.get("GITHUB_ACTIONS") else f"{kind}: {text}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait until a deploy is live at its public addresses.")
    parser.add_argument("--commit", required=True, help="the commit that was merged")
    parser.add_argument("--api", help="the backend's address")
    parser.add_argument("--web", help="the frontend's address")
    parser.add_argument("--since", help="when the merge was pushed (ISO 8601) — to say how long merge-to-live took")
    parser.add_argument("--timeout", type=float, default=600.0, help="give up after this many seconds (default 600)")
    parser.add_argument("--budget", type=float, default=300.0, help="merge-to-live should be within this (default 300)")
    args = parser.parse_args(argv)

    asked = addresses(args.api, args.web)
    if not asked:
        annotate(
            "notice", "No address to ask (PRODUCTION_API_URL / PRODUCTION_WEB_URL are not set): nothing was waited for."
        )
        return 0

    print(f"Waiting for {args.commit[:7]} at: " + ", ".join(asked.values()), flush=True)
    live, last = wait(asked, args.commit, args.timeout)

    if len(live) < len(asked):
        for name in asked:
            if name not in live:
                annotate(
                    "error",
                    f"{name} is not running {args.commit[:7]} after {args.timeout:.0f} s — last answer: {last[name]}",
                )
        return 1

    took = seconds_since(args.since)
    lines = [
        f"**{args.commit[:7]} is live** — "
        + ", ".join(f"{name} after {seconds:.0f} s" for name, seconds in live.items())
        + "."
    ]
    if took is not None:
        lines.append(f"From the merge to live: **{took:.0f} s** (the aim: within {args.budget:.0f} s).")
        if took > args.budget:
            annotate("warning", f"Merge to live took {took:.0f} s — longer than the {args.budget:.0f} s aimed for.")
    print("\n".join(lines), flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as page:
            page.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
