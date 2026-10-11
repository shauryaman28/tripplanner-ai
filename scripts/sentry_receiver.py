"""A local stand-in for Sentry: it receives the app's error reports and prints each one.

    python scripts/sentry_receiver.py                 # listens on http://localhost:9010
    python scripts/sentry_receiver.py --port 9011

Then give the backend a DSN that points at it — in .env, or for one run on the command line:

    SENTRY_DSN=http://standin@localhost:9010/1        # from a container: host.docker.internal

and make it fail (GET /admin/sentry-test, or anything that goes wrong). Each report is printed
as it arrives:

    ✗ DeliberateError: deliberate test error 7c1f…   production · tripplanner-ai@0.27.0 · GET /admin/sentry-test
    ~ trace  GET /trips/{trip_id}/status             production

And everything it was sent can be read back:

    curl  http://localhost:9010/events                # every error and trace received, as JSON
    curl 'http://localhost:9010/events?marker=7c1f'   # only those that mention 7c1f
    curl  http://localhost:9010/raw                   # every report as it arrived, decompressed:
                                                      # what would leave the machine

It is for trying error reporting without a Sentry account, and for reading exactly what a report
contains before a real DSN is ever set. It speaks the one request the SDK makes (POST
/api/<project>/envelope/), checks no key, keeps what it is sent in memory, and is not Sentry: no
grouping, no alerts, no UI. Do not put it on a network.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def decompressed(body: bytes, encoding: str) -> bytes:
    """A request body as the SDK compressed it."""
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding == "br":
        import brotli  # only installed where the SDK chose it

        return brotli.decompress(body)
    if encoding == "zstd":
        import zstandard

        return zstandard.ZstdDecompressor().decompressobj().decompress(body)
    return body


def parse_envelope(data: bytes) -> tuple[dict, list[tuple[dict, bytes]]]:
    """An envelope: one line of JSON about it, then its items — each a line of JSON and a payload."""
    first, _, rest = data.partition(b"\n")
    header = json.loads(first or b"{}")
    items: list[tuple[dict, bytes]] = []
    while rest.strip():
        line, _, rest = rest.partition(b"\n")
        if not line.strip():
            continue
        about = json.loads(line)
        length = about.get("length")
        if length is None:
            payload, _, rest = rest.partition(b"\n")
        else:
            payload, rest = rest[:length], rest[length:].removeprefix(b"\n")
        items.append((about, payload))
    return header, items


def summary(kind: str, payload: dict) -> str:
    """One line for one report."""
    where = " · ".join(
        str(part)
        for part in (
            payload.get("environment"),
            payload.get("release"),
            " ".join(str(x) for x in ((payload.get("request") or {}).get("method"), payload.get("transaction")) if x),
        )
        if part
    )
    if kind == "transaction":
        return f"~ trace  {payload.get('transaction') or '?':<44} {where}"
    errors = ((payload.get("exception") or {}).get("values")) or []
    if errors:
        what = f"{errors[-1].get('type')}: {errors[-1].get('value')}"
    else:
        entry = payload.get("logentry") or {}
        what = str(entry.get("formatted") or entry.get("message") or payload.get("message") or "?")
    tags = payload.get("tags") or {}
    tagged = "  [" + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())) + "]" if tags else ""
    return f"✗ {what.splitlines()[0][:110] if what else '?'}   {where}{tagged}"


class Receiver:
    """What has been received."""

    def __init__(self, out=sys.stdout) -> None:
        self.events: list[dict] = []  # {"type": "event" | "transaction" | …, "payload": {...}}
        self.raw: list[str] = []  # every envelope, decompressed, as text
        self._lock = threading.Lock()
        self._out = out

    def take(self, body: bytes, encoding: str = "") -> str | None:
        data = decompressed(body, encoding)
        header, items = parse_envelope(data)
        with self._lock:
            self.raw.append(data.decode("utf-8", errors="replace"))
            for about, payload in items:
                kind = str(about.get("type"))
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    parsed = {"unreadable": payload[:200].decode("utf-8", errors="replace")}
                self.events.append({"type": kind, "payload": parsed})
                if kind in ("event", "transaction") and isinstance(parsed, dict):
                    print(summary(kind, parsed), file=self._out, flush=True)
        return header.get("event_id")

    def matching(self, marker: str | None, kinds: tuple[str, ...] = ("event", "transaction")) -> list[dict]:
        with self._lock:
            found = [event for event in self.events if event["type"] in kinds]
        return [event for event in found if marker is None or marker in json.dumps(event, default=str)]


def handler_for(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # the reports are the output
            pass

        def _answer(self, status: int, body: object) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            path = urlsplit(self.path).path.rstrip("/")
            if not (path.endswith("/envelope") or path.endswith("/store")):
                self._answer(404, {"detail": "this stand-in takes POST /api/<project>/envelope/"})
                return
            try:
                event_id = receiver.take(body, self.headers.get("Content-Encoding") or "")
            except Exception as exc:  # an envelope it cannot read is said so, not swallowed
                print(f"! an envelope could not be read: {exc!r}", file=sys.stderr, flush=True)
                self._answer(400, {"detail": f"unreadable envelope: {exc!r}"})
                return
            self._answer(200, {"id": event_id})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            marker = (parse_qs(url.query).get("marker") or [None])[0]
            if url.path.rstrip("/") == "/events":
                self._answer(200, receiver.matching(marker))
            elif url.path.rstrip("/") == "/raw":
                with receiver._lock:
                    self._answer(200, list(receiver.raw))
            else:
                self._answer(200, {"what": "a local stand-in for Sentry", "events": "/events?marker=<text>"})

    return Handler


def serve(port: int, receiver: Receiver | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A server — started by the caller (`serve_forever`), stopped by `shutdown`."""
    return ThreadingHTTPServer((host, port), handler_for(receiver or Receiver()))


def main() -> None:
    parser = argparse.ArgumentParser(description="A local stand-in for Sentry: prints each error report it is sent.")
    parser.add_argument("--port", type=int, default=9010)
    parser.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: this machine only)")
    args = parser.parse_args()
    server = serve(args.port, host=args.host)
    print(
        f"Receiving error reports on http://localhost:{args.port} — set SENTRY_DSN=http://standin@localhost:{args.port}/1",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
