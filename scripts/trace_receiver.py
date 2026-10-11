"""A local stand-in for LangSmith: it receives the planner's traces and prints each one as a tree.

    python scripts/trace_receiver.py                 # listens on http://localhost:1984
    python scripts/trace_receiver.py --port 1985

Then point the backend at it — in .env, or for one run on the command line:

    LANGCHAIN_TRACING_V2=true
    LANGCHAIN_API_KEY=local                 # anything: it is not checked here
    LANGCHAIN_ENDPOINT=http://localhost:1984

and plan a trip. Each trace is printed when it has finished:

    plan trip   trip 1fee8655  user 3a2c91b0  turn 1                    9.9 s    5,412 tokens
    ├─ intent_parsing                                                   1.4 s      312 tokens
    │  └─ ChatGoogleGenerativeAI  [llm]                                 1.3 s      312 tokens
    ├─ run_flight                                                       0.1 s
    │  └─ flight_agent                                                  0.1 s
    …

What LangSmith's filter on metadata does, this does for one trip:

    curl 'http://localhost:1984/traces?trip_id=<the trip's id>'      # only that trip's traces, as JSON
    curl  http://localhost:1984/traces                               # every trace received
    curl  http://localhost:1984/records                              # every run exactly as it was sent:
                                                                     # what would leave the machine

It is for developing without a LangSmith account, and for seeing exactly what
the planner uploads before a key is ever put in .env. It keeps what it is sent
in memory, speaks the two requests the tracing library makes (GET /info,
POST /runs/batch), checks no key, and is not LangSmith: no UI, no search, no
storage. Do not put it on a network.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT)]

from src.ai.trace_tree import SERVER_INFO, Runs, Span, render  # noqa: E402

SETTLE_SECONDS = 1.0  # a trace's last spans can arrive a batch after its root has ended


def as_json(span: Span) -> dict:
    return {
        "name": span.name,
        "project": span.project,  # the LangSmith project the run was sent to: one, for every trace
        "run_type": span.run_type,
        "duration_ms": span.duration_ms,
        "tokens": span.tokens,
        "error": span.error,
        "metadata": span.metadata,
        "tags": span.tags,
        "spans": [as_json(child) for child in span.children],
    }


class Receiver:
    """What has been received, and which of it has been printed."""

    def __init__(self, out=sys.stdout) -> None:
        self.runs = Runs()
        self._printed: set[str] = set()
        self._lock = threading.Lock()
        self._out = out

    def take(self, batch: dict) -> None:
        self.runs.take(batch)
        threading.Timer(SETTLE_SECONDS, self.print_finished).start()

    def print_finished(self) -> None:
        """Print every trace that has ended and has not been printed yet."""
        with self._lock:
            for root in self.runs.traces():
                if root.ended is not None and root.id not in self._printed:
                    self._printed.add(root.id)
                    print("\n" + render(root), file=self._out, flush=True)


def handler_for(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # the traces are the output; the access log is noise
            pass

        def _answer(self, status: int, body: object) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path.rstrip("/") == "/info":
                self._answer(200, SERVER_INFO)
            elif url.path.rstrip("/") == "/traces":
                trip_id = (parse_qs(url.query).get("trip_id") or [None])[0]
                self._answer(200, [as_json(root) for root in receiver.runs.traces(trip_id)])
            elif url.path.rstrip("/") == "/records":
                self._answer(200, receiver.runs.records())
            else:
                self._answer(200, {"what": "a local stand-in for LangSmith", "traces": "/traces?trip_id=<id>"})

        def do_POST(self) -> None:
            url = urlsplit(self.path)
            body = self._body()
            if url.path.rstrip("/") == "/runs/batch":
                receiver.take(body)
            elif url.path.rstrip("/") == "/runs":  # a client told not to batch sends runs one at a time
                receiver.take({"post": [body]})
            self._answer(202, {})

        def do_PATCH(self) -> None:
            run_id = urlsplit(self.path).path.rstrip("/").rsplit("/", 1)[-1]
            receiver.take({"patch": [{**self._body(), "id": run_id}]})
            self._answer(202, {})

    return Handler


def serve(port: int, receiver: Receiver | None = None) -> ThreadingHTTPServer:
    """A server on 127.0.0.1 — started by the caller (`serve_forever`), stopped by `shutdown`."""
    return ThreadingHTTPServer(("127.0.0.1", port), handler_for(receiver or Receiver()))


def main() -> None:
    parser = argparse.ArgumentParser(description="A local stand-in for LangSmith: prints each trace it is sent.")
    parser.add_argument("--port", type=int, default=1984)
    port = parser.parse_args().port
    server = serve(port)
    print(f"Receiving traces on http://localhost:{port} — set LANGCHAIN_ENDPOINT to that, and plan a trip.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
