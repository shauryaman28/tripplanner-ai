"""The observability stack on this machine: up, down, and what it is doing (Phase 29).

    python scripts/observability.py up        # start Prometheus, Grafana, Jaeger, Loki and Alloy; wait for them
    python scripts/observability.py down      # stop them and delete what they kept
    python scripts/observability.py ps        # what is running
    python scripts/observability.py logs      # their own logs        (logs -f grafana to follow one)

What the stack is: docker/observability/compose.yml. Where to look once it is up:

    Grafana      http://localhost:3001        the dashboard; Explore for the log lines
    Prometheus   http://localhost:9090        Status → Targets says whether it can read the backend
    Jaeger       http://localhost:16686       the traces

The app is told about it by two settings (HOW_TO_RUN.md, Step 27):

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318      where traces go
    LOG_FORMAT=json                                        every log line an object with trip_id, user_id, trace_id

`up` also makes ./logs (a backend run on the host writes its lines there for the stack to read)
and copies the production-like stack's operations token to where Prometheus reads it — that
backend only shows its metrics to whoever has it. The copy is git-ignored.

Then, to see all of it work:    python scripts/observability_check.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "docker" / "observability"
COMPOSE = HERE / "compose.yml"
TOKEN_FOR_PROMETHEUS = HERE / "secrets" / "ops_token"
PRODLIKE_SECRETS = ROOT / ".env.prodlike"

# What has to answer before the stack is called up.
READY = {
    "Prometheus": "http://localhost:9090/-/ready",
    "Grafana": "http://localhost:3001/api/health",
    "Jaeger": "http://localhost:16686/",
    "Loki": "http://localhost:3110/ready",
}


def hand_over_the_token() -> None:
    """The production-like backend's OPS_TOKEN, as a file Prometheus reads. Empty when there is no such stack yet."""
    token = ""
    if PRODLIKE_SECRETS.exists():
        for line in PRODLIKE_SECRETS.read_text().splitlines():
            if line.startswith("PRODLIKE_OPS_TOKEN="):
                token = line.split("=", 1)[1].strip()
    TOKEN_FOR_PROMETHEUS.parent.mkdir(exist_ok=True)
    TOKEN_FOR_PROMETHEUS.write_text(token)
    TOKEN_FOR_PROMETHEUS.chmod(0o644)  # read by the container's own user, which is not this one


def compose(*args: str, check: bool = True) -> int:
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], cwd=ROOT, check=check).returncode


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as answer:
            return answer.status == 200
    except Exception:
        return False


def wait_until_ready(seconds: float = 180.0) -> list[str]:
    """Wait for every part. Returns the ones that never answered."""
    deadline = time.monotonic() + seconds
    waiting = dict(READY)
    while waiting and time.monotonic() < deadline:
        for name, url in list(waiting.items()):
            if answers(url):
                del waiting[name]
        if waiting:
            time.sleep(2)
    return sorted(waiting)


def main() -> int:
    parser = argparse.ArgumentParser(description="The observability stack on this machine.")
    parser.add_argument("what", choices=["up", "down", "logs", "ps"])
    parser.add_argument("rest", nargs="*", help="passed on to docker compose (e.g. -f grafana for logs)")
    args, extra = parser.parse_known_args()

    if args.what == "up":
        (ROOT / "logs").mkdir(exist_ok=True)
        hand_over_the_token()
        if compose("up", "-d", *args.rest, *extra, check=False) != 0:
            print("docker compose could not start the stack — its own message is above.")
            return 1
        print("waiting for Prometheus, Grafana, Jaeger and Loki to answer…", flush=True)
        silent = wait_until_ready()
        if silent:
            print(f"not answering: {', '.join(silent)}. Their logs: python scripts/observability.py logs")
            return 1
        print(
            "ready.\n"
            "  Grafana      http://localhost:3001\n"
            "  Prometheus   http://localhost:9090/targets\n"
            "  Jaeger       http://localhost:16686\n"
            "Tell the app:  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  LOG_FORMAT=json\n"
            "Next:          python scripts/observability_check.py"
        )
        return 0
    if args.what == "down":
        return compose("down", "-v", "--remove-orphans", *args.rest, *extra, check=False)
    if args.what == "logs":
        return compose("logs", "--tail", "100", *args.rest, *extra, check=False)
    return compose("ps", *args.rest, *extra, check=False)


if __name__ == "__main__":
    sys.exit(main())
