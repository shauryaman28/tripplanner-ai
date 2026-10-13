"""The far end of a model call that goes wrong: it says 429, it says 500, or it never answers (Phase 31).

A stand-in model that *raised* what a provider's client raises would be a guess at what that
client does first — how often it asks again, how long it waits between, when it gives up. That is
the thing a chaos exercise is run to find out. So when a fault is on (faults.py), the model is the
provider's real client, with the timeout and the retries the app gave it, and this is the server
it is pointed at:

    /429/<anything>      429, in the provider's own words — Gemini's quota error with its RetryInfo,
                         Groq's rate limit with its Retry-After
    /500/<anything>      500
    /hang/<anything>     the request is read, and nothing is ever sent back
    GET  /log            every request it has had: when, what for, what it did, how long the client stayed
    POST /reset          forget them

It answers nothing correctly: a model that works is a stand-in (models.py), not this.

    python -m loadtest.standins.fault_server          # port 9099, or $PORT
"""

from __future__ import annotations

import json
import os
import time

FAULTS = ("429", "500", "hang")
RETRY_AFTER_S = 2  # what Groq's 429 tells its client to wait
RETRY_DELAY = "36s"  # what Gemini's free tier says when the day's requests are spent

_seen: list[dict] = []


def provider_of(path: str) -> str:
    """Whose API a request was for, from its address: the two clients ask for different things."""
    if "generateContent" in path or "/models/" in path:
        return "gemini"
    if "chat/completions" in path:
        return "groq"
    return "unknown"


def said(fault: str, provider: str) -> tuple[int, dict[str, str], dict]:
    """(status, headers, body) for a fault that answers — worded as that provider words it."""
    status = int(fault)
    if provider == "gemini":
        error: dict = {"code": status, "message": "An internal error has occurred.", "status": "INTERNAL"}
        if status == 429:
            error = {
                "code": 429,
                "message": (
                    "You exceeded your current quota, please check your plan and billing details. "
                    "Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                    f"limit: 20. Please retry in {RETRY_DELAY}."
                ),
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaValue": "20",
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": RETRY_DELAY},
                ],
            }
        return status, {}, {"error": error}
    if status == 429:
        return (
            429,
            {"retry-after": str(RETRY_AFTER_S)},
            {
                "error": {
                    "message": f"Rate limit reached for this model on tokens per minute. Please try again in {RETRY_AFTER_S}s.",
                    "type": "tokens",
                    "code": "rate_limit_exceeded",
                }
            },
        )
    return status, {}, {"error": {"message": "Internal Server Error", "type": "internal_server_error"}}


async def _answer(send, status: int, body: dict | list, headers: dict[str, str] | None = None) -> None:
    payload = json.dumps(body).encode()
    sent = {"content-type": "application/json", "content-length": str(len(payload)), **(headers or {})}
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(name.encode(), value.encode()) for name, value in sent.items()],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def app(scope, receive, send) -> None:
    """One ASGI app, no framework: three ways of failing, and a record of who asked."""
    if scope["type"] != "http":
        return
    path, method = scope["path"], scope["method"]
    if path == "/log":
        return await _answer(send, 200, _seen)
    if path == "/reset":
        _seen.clear()
        return await _answer(send, 200, {"reset": True})

    fault = path.strip("/").split("/", 1)[0]
    if fault not in FAULTS:
        return await _answer(
            send, 404, {"error": f"this server only fails: {', '.join('/' + name for name in FAULTS)}"}
        )

    while True:  # the request, whole — a client that is still sending has not been refused anything yet
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            break
    note = {"at": round(time.time(), 3), "method": method, "path": path, "did": fault, "provider": provider_of(path)}
    _seen.append(note)

    if fault == "hang":
        arrived = time.monotonic()
        while (await receive())["type"] != "http.disconnect":  # nothing is sent: only the client can end this
            pass
        note["client_left_after_s"] = round(time.monotonic() - arrived, 2)
        return
    status, headers, body = said(fault, note["provider"])
    await _answer(send, status, body, headers)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9099")), log_level="warning")


if __name__ == "__main__":
    main()
