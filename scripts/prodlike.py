"""The production-like stack on this machine: up, down, and what it is doing (Phase 27).

    python scripts/prodlike.py up        # build the image, start the stack, wait until it is ready
    python scripts/prodlike.py down      # stop it and delete everything it made
    python scripts/prodlike.py logs      # the backend's log        (logs -f to follow)
    python scripts/prodlike.py ps        # what is running

What the stack is, and why each part is the way it is: docker/prodlike/compose.yml. In short — the
image that gets deployed, started the way a platform starts it, against a Postgres that only talks
TLS behind a connection pooler, and a Redis that only talks TLS and wants a password.

    backend   http://localhost:8200     (the port a "platform" told it to listen on)
    pooler    localhost:6543            (the database's pooled address)
    postgres  localhost:5544            (its direct one)
    redis     rediss://localhost:6390

`up` writes .env.prodlike the first time: the two secrets the backend must be given (its signing
secret and its operations token), made fresh — the repo's defaults would be refused, as they would
be in production. The file is git-ignored. The provider keys are read from .env.

Then, to see all of it work:    python scripts/prod_check.py --prodlike
"""

from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker" / "prodlike" / "compose.yml"
SECRETS = ROOT / ".env.prodlike"
API = "http://localhost:8200"


def make_secrets() -> None:
    if SECRETS.exists():
        return
    SECRETS.write_text(
        "# Made by scripts/prodlike.py for the production-like stack on this machine. Not for anywhere else.\n"
        f"PRODLIKE_JWT_SECRET={secrets.token_hex(32)}\n"
        f"PRODLIKE_OPS_TOKEN={secrets.token_hex(32)}\n"
    )
    print(f"wrote {SECRETS.name} (git-ignored): a signing secret and an operations token for this stack")


def compose(*args: str, check: bool = True) -> int:
    command = ["docker", "compose", "-f", str(COMPOSE)]
    for env_file in (ROOT / ".env", SECRETS):
        if env_file.exists():
            command += ["--env-file", str(env_file)]
    return subprocess.run([*command, *args], cwd=ROOT, check=check).returncode


def wait_until_ready(seconds: float = 180.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/ready", timeout=3) as answer:
                if answer.status == 200:
                    return True
        except Exception:
            pass
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "tripplanner-prodlike-backend-1"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if state == "exited":
            return False
        time.sleep(2)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="The production-like stack on this machine.")
    parser.add_argument("what", choices=["up", "down", "logs", "ps"])
    parser.add_argument("rest", nargs="*", help="passed on to docker compose (e.g. -f for logs)")
    args, extra = parser.parse_known_args()

    if args.what == "up":
        make_secrets()
        if compose("up", "-d", "--build", *args.rest, *extra, check=False) != 0:
            print("docker compose could not start the stack — its own message is above.")
            return 1
        print("waiting for the backend to say it is ready…", flush=True)
        if wait_until_ready():
            print(f"ready: {API}/ready answers 200.  Next: python scripts/prod_check.py --prodlike")
            return 0
        print("the backend did not become ready. Its log:\n", flush=True)
        compose("logs", "--tail", "40", "backend", check=False)
        return 1
    if args.what == "down":
        return compose("down", "-v", "--remove-orphans", *args.rest, *extra, check=False)
    if args.what == "logs":
        return compose("logs", *(args.rest or []), *extra, "backend", check=False)
    return compose("ps", *args.rest, *extra, check=False)


if __name__ == "__main__":
    sys.exit(main())
