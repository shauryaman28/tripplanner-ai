"""A local stand-in for wherever alerts go — Slack, e-mail, a pager: it prints each alert it is sent (Phase 34).

    python scripts/alert_receiver.py                  # listens on http://localhost:9011
    python scripts/alert_receiver.py --host 0.0.0.0   # as the observability stack runs it, in a container

Alertmanager (docker/observability/alertmanager.yml) sends it what Prometheus says fires, and each
alert is printed as the person on call would read it:

    ▲ FIRING   EndpointErrorRateHigh · critical · tripplanner-prodlike · POST /trips/{trip_id}/plan
               POST /trips/{trip_id}/plan — 100% of its requests failed over the last 5 minutes; …
               Metric: http_requests_total{status="5xx"} ÷ all requests to POST /trips/{trip_id}/plan, over 5m.
               Now: 100% · threshold: 5% · backend: tripplanner-prodlike.
               …
               Runbook: https://github.com/…/docs/runbook.md#endpointerrorratehigh
    ✓ RESOLVED EndpointErrorRateHigh · critical · tripplanner-prodlike · POST /trips/{trip_id}/plan  (fired 14:02:10)

    python scripts/observability.py logs -f alert-receiver      # to watch them arrive

What it was sent can be read back — which is how scripts/alert_drill.py knows an alert reached
somebody, and what it said:

    curl  http://localhost:9011/alerts                          # every notification, as Alertmanager sent it
    curl 'http://localhost:9011/alerts?alertname=CacheHitRatioLow&status=firing'
    curl -X POST http://localhost:9011/reset                    # forget them

It speaks Alertmanager's webhook (POST /alerts, version 4), keeps what it is sent in memory, checks no
key, and is not a pager: nobody is woken. Standard library only — the stack runs it in a plain Python
image. Do not put it on a network.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

INDENT = " " * 11


def _clock(when: str | None) -> str:
    """An RFC 3339 time as HH:MM:SS (UTC), or '?' for none — Alertmanager's "0001-01-01…" is none."""
    if not when or when.startswith("0001-"):
        return "?"
    try:
        return datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return when


def lines_for(alert: dict) -> list[str]:
    """One alert of a notification, as it is printed: a heading, and for a firing one its message."""
    labels, notes = alert.get("labels") or {}, alert.get("annotations") or {}
    firing = alert.get("status") == "firing"
    where = " · ".join(
        str(part)
        for part in (
            labels.get("alertname", "?"),
            labels.get("severity"),
            labels.get("job"),
            labels.get("endpoint") or labels.get("metric"),
        )
        if part
    )
    if not firing:
        return [f"✓ RESOLVED {where}  (fired {_clock(alert.get('startsAt'))}, resolved {_clock(alert.get('endsAt'))})"]
    out = [f"▲ FIRING   {where}"]
    for text in (notes.get("summary"), notes.get("description")):
        out.extend(f"{INDENT}{line}" for line in (text or "").splitlines() if line.strip())
    if notes.get("runbook_url"):
        out.append(f"{INDENT}Runbook: {notes['runbook_url']}")
    return out


class Receiver:
    """Every notification received, in order."""

    def __init__(self, out=sys.stdout) -> None:
        self.notifications: list[dict] = []
        self._lock = threading.Lock()
        self._out = out

    def take(self, notification: dict) -> int:
        """One webhook call from Alertmanager. Returns how many alerts it carried."""
        if not isinstance(notification, dict) or not isinstance(notification.get("alerts"), list):
            raise ValueError("not an Alertmanager notification: no list of alerts")
        kept = {**notification, "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self._lock:
            self.notifications.append(kept)
            for alert in notification["alerts"]:
                print("\n".join(lines_for(alert)), file=self._out, flush=True)
        return len(notification["alerts"])

    def matching(self, alertname: str | None = None, status: str | None = None) -> list[dict]:
        """The notifications with at least one alert of that name and that status — that alert's alone."""
        with self._lock:
            kept = list(self.notifications)
        found = []
        for notification in kept:
            alerts = [
                alert
                for alert in notification["alerts"]
                if (alertname is None or (alert.get("labels") or {}).get("alertname") == alertname)
                and (status is None or alert.get("status") == status)
            ]
            if alerts:
                found.append({**notification, "alerts": alerts})
        return found

    def forget(self) -> None:
        with self._lock:
            self.notifications.clear()


def handler_for(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # the alerts are the output
            pass

        def _answer(self, status: int, body: object) -> None:
            data = json.dumps(body, default=str, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path.rstrip("/")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path == "/reset":
                receiver.forget()
                self._answer(200, {"forgotten": True})
                return
            if path != "/alerts":
                self._answer(404, {"detail": "this stand-in takes Alertmanager's webhook at POST /alerts"})
                return
            try:
                count = receiver.take(json.loads(body or b"{}"))
            except ValueError as exc:  # a notification it cannot read is said so, not swallowed
                print(f"! a notification could not be read: {exc}", file=sys.stderr, flush=True)
                self._answer(400, {"detail": str(exc)})
                return
            self._answer(200, {"alerts": count})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            asked = {key: values[0] for key, values in parse_qs(url.query).items()}
            if url.path.rstrip("/") == "/alerts":
                self._answer(200, receiver.matching(asked.get("alertname"), asked.get("status")))
            else:
                self._answer(200, {"what": "a local stand-in for where alerts go", "alerts": "/alerts?alertname=…"})

    return Handler


def serve(port: int, receiver: Receiver | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """A server — started by the caller (`serve_forever`), stopped by `shutdown`."""
    return ThreadingHTTPServer((host, port), handler_for(receiver or Receiver()))


def main() -> None:
    parser = argparse.ArgumentParser(description="A local stand-in for where alerts go: prints each one it is sent.")
    parser.add_argument("--port", type=int, default=9011)
    parser.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: this machine only)")
    args = parser.parse_args()
    server = serve(args.port, host=args.host)
    print(f"Receiving alerts on http://{args.host}:{args.port}/alerts", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
