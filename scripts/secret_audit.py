"""Is a key anywhere it should not be? The logs, the repository, its history (Phase 35).

    python scripts/secret_audit.py logs                 # ./logs, .evals/, docs/, Loki (if it is up), the containers'
    python scripts/secret_audit.py logs some/file.log   # …or these files and directories
    python scripts/secret_audit.py repo                 # every tracked file, and every commit of every branch

Two ways of finding a key, both needed:

    by value   every secret in .env and .env.prodlike (a setting whose name says KEY, SECRET, TOKEN,
               PASSWORD; the passwords inside the database and Redis URLs) — found wherever it is,
               whatever it looks like. OpenTripMap's and OpenWeather's keys are bare hex: no pattern
               tells them from a trace id or a cache key's digest; only their values can.
    by shape   what a provider's key looks like — Google's `AIza…` and its newer `AQ.…`, Groq's `gsk_`,
               Duffel's `duffel_test_`/`duffel_live_`, LangSmith's `lsv2_`, LiteAPI's `sand_`/`prod_`,
               Anthropic's `sk-ant-`, OpenAI's `sk-`; a hex key after `appid=` / `apikey=` / `key=`;
               an Amadeus client id after `client_id=`; a login token (a JWT), `Bearer …`. A key that is
               not this machine's — an old one, somebody else's — is found this way.

Nothing found is ever printed: a finding is its place (file and line, or commit and file), what it
matched (the setting's name, or the shape's), and the first four characters. Exit code 0 when
nothing was found, 1 when something was.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = (".env", ".env.prodlike", ".env.production")
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CLIENT_ID", re.I)
URL_NAMES = ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "REDIS_URL", "PRODLIKE_DATABASE_URL", "PRODLIKE_REDIS_URL")
MIN_VALUE = 8  # shorter values are words, not secrets: "postgres" is in every sentence about the database
# What a stand-in stack is given instead of a key — said in the open, on purpose (docker/loadtest/compose.yml).
NOT_SECRETS = frozenset({"standin-not-a-key", "change-me-before-production", "ci-only-password", "local"})
PUBLISHED = ("docker-compose.yml", ".env.example")

SHAPES = {
    "a Google API key (AIza…)": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "a Google AI Studio key (AQ.…)": re.compile(r"\bAQ\.[0-9A-Za-z_\-]{40,}"),
    "a Groq key (gsk_…)": re.compile(r"\bgsk_[0-9A-Za-z]{40,}"),
    "a Duffel token (duffel_…)": re.compile(r"\bduffel_(?:test|live)_[0-9A-Za-z_\-]{20,}"),
    "a LangSmith key (lsv2_…)": re.compile(r"\blsv2_(?:pt|sk)_[0-9a-f]{20,}_[0-9a-f]{6,}"),
    "a LiteAPI key (sand_/prod_…)": re.compile(
        r"\b(?:sand|prod)_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    ),
    "an Anthropic key (sk-ant-…)": re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),
    "an OpenAI key (sk-…)": re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_\-]{32,}"),
    "a hex key in a query string": re.compile(r"(?i)\b(?:appid|apikey|api_key|key)=([0-9a-f]{32,64})\b"),
    "an Amadeus client id": re.compile(r"(?i)\bclient_id=([0-9A-Za-z]{24,40})\b"),
    "a login token (JWT)": re.compile(r"\beyJ[0-9A-Za-z_\-]{8,}\.eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{16,}"),
    "a Bearer token": re.compile(r"(?i)\bbearer\s+[0-9A-Za-z._~+/\-]{20,}"),
}
# Where a shape is expected and is not a leak: test fixtures that show the shape on purpose, this file.
SHAPES_EXPECTED_IN = ("tests/", "scripts/secret_audit.py", "src/frontend/e2e/")


@dataclass(frozen=True)
class Finding:
    where: str  # "path:line", or "commit:path"
    what: str  # the setting's name, or the shape's
    starts: str  # the first four characters — enough to recognise it, not to use it

    def __str__(self) -> str:
        return f"  ✗ {self.where}  {self.what}  ({self.starts}…)"


def published_defaults() -> str:
    """What the repository itself gives as a default — the local database's password in docker-compose.yml and
    .env.example: a value written there is no secret, wherever else it turns up."""
    return "\n".join((ROOT / name).read_text() for name in PUBLISHED if (ROOT / name).exists())


def secrets_from_env_files() -> dict[str, str]:
    """{value: the setting's name} for every secret of this machine's env files. Never printed."""
    found: dict[str, str] = {}
    public = published_defaults()
    for name in ENV_FILES:
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("'\"")
            if not value or value in NOT_SECRETS or value in public:
                continue
            if SECRET_NAME.search(key) and len(value) >= MIN_VALUE:
                found[value] = f"{key} ({name})"
            if key in URL_NAMES:
                password = urllib.parse.urlsplit(value).password
                if password and len(password) >= MIN_VALUE and password not in NOT_SECRETS and password not in public:
                    found[password] = f"the password in {key} ({name})"
    return found


def scan_text(text: str, where: str, secrets: dict[str, str], shapes: bool = True) -> Iterator[Finding]:
    for number, line in enumerate(text.splitlines(), start=1):
        for value, name in secrets.items():
            if value in line:
                yield Finding(f"{where}:{number}", name, value[:4])
        if not shapes:
            continue
        for shape, pattern in SHAPES.items():
            for match in pattern.finditer(line):
                hit = match.group(match.lastindex or 0)
                if hit not in secrets:  # already said, by value
                    yield Finding(f"{where}:{number}", shape, hit[:4])


def files_under(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not any(part in ("node_modules", ".git", "__pycache__") for part in child.parts):
                    yield child


def loki_lines(base: str = "http://localhost:3110", days: int = 7) -> Iterator[tuple[str, str]]:
    """Every line Loki holds for the app, an hour at a time — less when an hour holds more than one answer can
    (5,000 lines) or takes too long to answer."""
    import socket
    import time

    end = int(time.time())
    oldest = end - days * 86_400
    step = 3_600
    while end > oldest:
        query = urllib.parse.urlencode(
            {"query": '{job="tripplanner"}', "start": f"{end - step}000000000", "end": f"{end}000000000", "limit": 5000}
        )
        try:
            with urllib.request.urlopen(f"{base}/loki/api/v1/query_range?{query}", timeout=60) as answer:
                result = json.load(answer)["data"]["result"]
        except (TimeoutError, socket.timeout):
            if step <= 60:
                raise
            step //= 4
            continue
        lines = [(stream["stream"].get("service", "?"), line) for stream in result for _, line in stream["values"]]
        if len(lines) >= 5000 and step > 60:  # too many for one answer: ask again in smaller pieces
            step //= 4
            continue
        yield from lines
        end -= step
        step = 3_600


def audit_logs(paths: list[Path], secrets: dict[str, str]) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    looked: dict[str, int] = {}
    for path in files_under(paths):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        where = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        looked[where] = text.count("\n") + 1
        expected = where.startswith(SHAPES_EXPECTED_IN)
        findings += scan_text(text, where, secrets, shapes=not expected)
    return findings, looked


def audit_loki(secrets: dict[str, str]) -> tuple[list[Finding], int] | None:
    try:
        lines = list(loki_lines())
    except Exception:
        return None
    findings = [
        finding
        for number, (service, line) in enumerate(lines)
        for finding in scan_text(line, f"loki[{service}]#{number}", secrets)
    ]
    return findings, len(lines)


def audit_containers(secrets: dict[str, str]) -> tuple[list[Finding], int]:
    """The logs Docker keeps of every container running now."""
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    findings: list[Finding] = []
    count = 0
    for name in names:
        out = subprocess.run(["docker", "logs", name], capture_output=True, text=True, errors="replace")
        text = out.stdout + out.stderr
        count += text.count("\n")
        findings += scan_text(text, f"docker logs {name}", secrets)
    return findings, count


def audit_repo(secrets: dict[str, str]) -> tuple[list[Finding], int, int]:
    """Every tracked file now, and every version of every file in every commit of every branch."""
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
    findings: list[Finding] = []
    for name in filter(None, tracked):
        path = ROOT / name
        if path.is_file():
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            findings += scan_text(text, name, secrets, shapes=not name.startswith(SHAPES_EXPECTED_IN))
    commits = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    # every blob any commit ever had, each read once
    objects = subprocess.run(["git", "rev-list", "--all", "--objects"], cwd=ROOT, capture_output=True, text=True).stdout
    blobs = {}
    for line in objects.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            blobs.setdefault(sha, path)
    batch = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=ROOT, input="\n".join(blobs).encode(), capture_output=True
    ).stdout
    position = 0
    for sha in blobs:
        header_end = batch.index(b"\n", position)
        _, kind, size = batch[position:header_end].decode().split()
        body = batch[header_end + 1 : header_end + 1 + int(size)]
        position = header_end + 1 + int(size) + 1
        if kind != "blob":
            continue
        text = body.decode(errors="replace")
        path = blobs[sha]
        for finding in scan_text(text, f"history {sha[:10]}:{path}", secrets, shapes=False):
            findings.append(finding)
    return findings, len(commits), len(blobs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Is a key anywhere it should not be?")
    parser.add_argument("what", choices=["logs", "repo"])
    parser.add_argument("paths", nargs="*", type=Path, help="logs: files or directories (default: the usual places)")
    args = parser.parse_args()
    secrets = secrets_from_env_files()
    print(
        f"{len(secrets)} secret values from {', '.join(n for n in ENV_FILES if (ROOT / n).exists())}; {len(SHAPES)} shapes"
    )

    findings: list[Finding] = []
    if args.what == "repo":
        found, commits, blobs = audit_repo(secrets)
        print(f"tracked files, and {blobs} versions of files across {commits} commits")
        findings += found
    else:
        paths = args.paths or [ROOT / "logs", ROOT / ".evals", ROOT / "docs"]
        found, looked = audit_logs(paths, secrets)
        findings += found
        print(f"{len(looked)} files, {sum(looked.values()):,} lines")
        if not args.paths:
            from_loki = audit_loki(secrets)
            if from_loki is None:
                print("Loki: not reachable (python scripts/observability.py up) — not looked at")
            else:
                findings += from_loki[0]
                print(f"Loki: {from_loki[1]:,} lines")
            from_docker, lines = audit_containers(secrets)
            findings += from_docker
            print(f"the running containers' logs: {lines:,} lines")

    for finding in findings:
        print(finding)
    print("nothing found" if not findings else f"{len(findings)} found")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
