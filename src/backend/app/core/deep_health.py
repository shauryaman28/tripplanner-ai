"""GET /admin/health-deep — every dependency asked to do its work once, and what each said (Phase 34).

    postgres    SELECT 1, through the app's own pool                                     needed for everything
    redis       PING, through the app's own client                                       a plan's conversation, the streams
    tool_server estimate_budget, through the app's own tool client — the process is      every search
                started if it is not running, as a plan would start it
    langsmith   the key shown to LangSmith (GET /api/v1/sessions?limit=1)                traces; a plan is made without

/ping says whether Postgres and Redis answer. This says whether the app can *work*: a tool server
that does not answer fails every plan while /ping says "ok", and so does one whose pool is used up
— a SELECT 1 that waits for a connection is as slow as the requests waiting behind it. Each check is
timed, has a deadline of its own, and runs beside the others: one dependency that hangs is reported
as hanging, and the answer still comes within the longest deadline (the tool server's, 10 s).

    {"status": "ok" | "degraded" | "failing", "checked_at": …, "took_ms": …,
     "dependencies": {"postgres": {"status": "ok", "required": true, "latency_ms": 2.1, "detail": "SELECT 1 answered"}, …}}

`failing`: something a plan needs is not working — HTTP 503, so that a monitor that reads only the
status can alert on it. `degraded`: only LangSmith is (plans are made, untraced) — 200. A dependency
that is not set up is `off`, not failing: LangSmith with no key, or anywhere APP_ENV is test (the
test stacks never call it).

What a failing check says is the error itself, with every secret the settings hold taken out: this
answers the operator (OPS_TOKEN), and "the pool timed out" is what they came for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LANGSMITH_API = "https://api.smith.langchain.com"
# A whole budget of the kind a plan asks for: arithmetic only, no provider behind it — the tool server
# itself is what is being asked.
BUDGET_QUESTION = {"flights": 12_000, "hotels": 4_000, "days": 3, "nights": 2, "daily_spend": 2_500, "group_size": 2}


class Off(Exception):
    """A dependency this deployment has not set up: not a failure."""


@dataclass(frozen=True)
class Check:
    name: str
    required: bool  # a plan cannot be made without it
    timeout_s: float
    run: Callable[[], Awaitable[str]]  # what it found, in a few words; raises when it is not working


async def _postgres() -> str:
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        answer = (await session.execute(text("SELECT 1"))).scalar()
    if answer != 1:
        raise RuntimeError(f"SELECT 1 answered {answer!r}")
    return "SELECT 1 answered"


async def _redis() -> str:
    from app.db.redis import get_redis

    client = await get_redis()  # RuntimeError when the app never got a client at startup
    if not await client.ping():
        raise RuntimeError("PING answered nothing")
    return "PING answered"


async def _tool_server() -> str:
    from src.ai.mcp_client.client import call_tool
    from src.ai.mcp_server.models import ToolError

    answer = await call_tool("estimate_budget", BUDGET_QUESTION)
    if isinstance(answer, ToolError):
        raise RuntimeError(f"{answer.code}: {answer.error}")
    total = answer.get("total") if isinstance(answer, dict) else None
    if not isinstance(total, (int, float)):
        raise RuntimeError(f"estimate_budget answered without a total: {str(answer)[:120]}")
    return f"estimate_budget answered: ₹{total:,.0f} for the test trip"


def _langsmith_client() -> httpx.AsyncClient:
    """What LangSmith is asked through. (The tests hand in one that answers for it.)"""
    return httpx.AsyncClient()


def _langsmith(settings: Any) -> Callable[[], Awaitable[str]]:
    async def run() -> str:
        from src.ai import tracing

        if settings.APP_ENV == "test":
            raise Off("APP_ENV is test: LangSmith is never called")
        if not settings.LANGCHAIN_API_KEY:
            raise Off("no LANGCHAIN_API_KEY: traces are not sent")
        base = (settings.LANGCHAIN_ENDPOINT or LANGSMITH_API).rstrip("/")
        async with _langsmith_client() as asking:
            answer = await asking.get(
                f"{base}/api/v1/sessions", params={"limit": 1}, headers={"x-api-key": settings.LANGCHAIN_API_KEY}
            )
        if answer.status_code in (401, 403):
            raise RuntimeError(f"the key was refused (HTTP {answer.status_code})")
        if answer.status_code != 200:
            raise RuntimeError(f"HTTP {answer.status_code} from {base}")
        tracing_is = (
            f"tracing on, project {settings.LANGCHAIN_PROJECT!r}"
            if tracing.enabled()
            else "tracing off (LANGCHAIN_TRACING_V2 is not true)"
        )
        return f"the key is accepted · {tracing_is}"

    return run


def checks(settings: Any) -> list[Check]:
    return [
        Check("postgres", True, 3.0, _postgres),
        Check("redis", True, 2.0, _redis),
        Check("tool_server", True, 10.0, _tool_server),
        Check("langsmith", False, 5.0, _langsmith(settings)),
    ]


async def _one(check: Check, scrub: Callable[[str], str]) -> dict:
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(check.run(), timeout=check.timeout_s)
        status = "ok"
    except Off as off:
        status, detail = "off", str(off)
    except asyncio.TimeoutError:
        status, detail = "failing", f"no answer within {check.timeout_s:g} s"
    except Exception as exc:  # whatever it was, it is the answer: this endpoint reports, it does not fail
        status, detail = "failing", f"{type(exc).__name__}: {exc}"[:300]
    took = round((time.perf_counter() - started) * 1000, 1)
    if status == "failing":
        level = logging.WARNING if check.required else logging.INFO
        logger.log(level, "[HEALTH] %s is failing: %s", check.name, scrub(detail))
    return {
        "status": status,
        "required": check.required,
        "latency_ms": took if status != "off" else None,
        "detail": scrub(detail),
    }


def overall(dependencies: dict[str, dict]) -> str:
    """failing: something a plan needs; degraded: only something it can do without; ok: neither."""
    failing = [result for result in dependencies.values() if result["status"] == "failing"]
    if any(result["required"] for result in failing):
        return "failing"
    return "degraded" if failing else "ok"


async def report(settings: Any, scrub: Callable[[str], str], every: list[Check] | None = None) -> dict:
    """Every check at once; the whole answer as GET /admin/health-deep gives it."""
    every = every if every is not None else checks(settings)
    started = time.perf_counter()
    results = await asyncio.gather(*(_one(check, scrub) for check in every))
    dependencies = {check.name: result for check, result in zip(every, results)}
    return {
        "status": overall(dependencies),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
        "dependencies": dependencies,
    }
