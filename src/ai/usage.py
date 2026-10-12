"""What a stretch of work asked of the models, and what that costs (Phase 26).

    with metered() as usage:
        ...anything that calls a model, however deep...
    usage.token_count, usage.cost_usd

Every reply a LangChain chat model gives carries its token counts. One callback
handler — attached to every model call in the process, by LangChain's own hook
— reads them and credits them to the meter that is open *where the model was
asked*. `run_logger.timed_run()` opens one around each agent's work, so each
`agent_runs` row gets exactly the tokens its agent spent.

"Where the model was asked" is the innermost open meter, and only that one. The
orchestrator times a whole run, and inside it each agent times its own part: a
callback that also credited the outer meter would count every token twice.
(LangChain's stock `get_usage_metadata_callback` does credit both, whenever the
inner call is made under a LangChain run the outer one started — found on the
first probe, and the reason this module exists.)

The prices are list prices in US dollars per million tokens. On a free tier
nothing is charged: `cost_usd` is what the tokens would cost, which is what a
budget has to be planned around. A model with no price on record has its
tokens counted and its cost left out — never guessed.

Phase 29: the same handler is where a model call is counted for Prometheus
(`llm_calls_total{agent, model, status}`, its tokens and their price) and made
a span of the trace under way. The agent is the one whose meter is open:
`metered("flight_agent")`. The model in the label is the one that was *asked
for* — a provider answers under a dated name of its own, and a call that failed
has no answer to read a name from.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.tracers.context import register_configure_hook
from opentelemetry.trace import SpanKind

from src.telemetry import metrics, traces

try:
    from app.core.config import settings
except ImportError:
    from src.backend.app.core.config import settings

logger = logging.getLogger(__name__)

# USD per million tokens: (input, output). Reasoning tokens are output tokens, and are billed as such.
#
# Read on 2026-10-11 from the providers' own pages — Groq: console.groq.com/docs/models and each
# model's page; Gemini: ai.google.dev/gemini-api/docs/pricing, paid tier, output including thinking
# tokens — and, for the two models a plan asks, checked against what LangSmith itself priced each
# call of a live one at: the same to the last digit. (The first table came from price trackers. It
# had gpt-oss-120b's output at $0.75 and gpt-oss-20b at $0.10 / $0.50 — a builder's cost a fifth too
# high.) Prices move: correct one with LLM_PRICES in .env, no code change. Cached input is cheaper
# at both providers and is not modelled.
# gemini-3.5-flash, the default GEMINI_MODEL, is not here: the API still serves it, but Google's
# price list no longer names it.
PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}

_unpriced: set[str] = set()  # models already warned about — once each, not once per call


def price_of(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens for a model as its provider names it; None if unknown.

    A dated or prefixed name is the model it is a version of: "models/gemini-3.5-flash-lite-001"
    is priced as "gemini-3.5-flash-lite" — the longest name on record that it starts with.
    """
    known = {**PRICES, **settings.LLM_PRICES}
    name = model.removeprefix("models/")
    if name in known:
        return tuple(known[name])
    family = max((key for key in known if name.startswith(key)), key=len, default=None)
    return tuple(known[family]) if family else None


class Usage:
    """The tokens asked of each model over a stretch of work. Safe to add to from several tasks or threads.

    `agent`: whose work it is — what its model calls are counted under (Phase 29).
    """

    def __init__(self, agent: str | None = None) -> None:
        self.agent = agent
        self.by_model: dict[str, list[int]] = {}  # model → [input tokens, output tokens]
        self._lock = threading.Lock()

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            counted = self.by_model.setdefault(model, [0, 0])
            counted[0] += int(input_tokens or 0)
            counted[1] += int(output_tokens or 0)

    @property
    def token_count(self) -> int | None:
        """Every token in and out, of every model; None when no model was asked — which is not 0 tokens."""
        with self._lock:
            return sum(sum(counted) for counted in self.by_model.values()) if self.by_model else None

    @property
    def cost_usd(self) -> float | None:
        """What those tokens cost at list price; None when no model was asked or none of them has a price.

        A model without a price is left out of the sum and named in the log, once.
        """
        with self._lock:
            used = {model: tuple(counted) for model, counted in self.by_model.items()}
        total, priced = 0.0, False
        for model, (tokens_in, tokens_out) in used.items():
            price = price_of(model)
            if price is None:
                if model not in _unpriced:
                    _unpriced.add(model)
                    logger.warning(
                        "[COST] no price on record for model %r — its tokens are counted, its cost is not. "
                        "Add one with LLM_PRICES in .env.",
                        model,
                    )
                continue
            priced = True
            total += (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
        return round(total, 8) if priced else None


# The meter open in the current context — the innermost one, when they are nested.
_meter: ContextVar[Usage | None] = ContextVar("usage_meter", default=None)


MAX_CALLS_UNDER_WAY = 1000  # a call whose end was never reported is forgotten rather than kept for ever


def _asked_for(kwargs: dict) -> str:
    """The model a call was made to, as it was asked for — LangChain names it for every chat model."""
    named = (kwargs.get("metadata") or {}).get("ls_model_name")
    params = kwargs.get("invocation_params") or {}
    return str(named or params.get("model") or params.get("model_name") or "unknown").removeprefix("models/")


class _Recorder(BaseCallbackHandler):
    """Credits each model reply's tokens to the meter open where the model was asked.

    Phase 29: and counts the call, and makes it a span — begun when the model is asked, ended when
    it answers or fails. Nothing here may fail a model call: LangChain logs what a handler raises
    and goes on.
    """

    run_inline = True  # called in the asking task itself: no trip through a thread pool to add two numbers

    def __init__(self) -> None:
        self._under_way: dict[Any, tuple[str, str, Any]] = {}  # run id → (agent, model asked for, its span)

    def _begin(self, run_id: Any, kwargs: dict) -> None:
        meter = _meter.get()
        agent = (meter.agent if meter is not None else None) or "unmetered"
        model = _asked_for(kwargs)
        span = traces.start(
            f"llm {model}",
            {"gen_ai.operation.name": "chat", "gen_ai.request.model": model, "agent.name": agent},
            kind=SpanKind.CLIENT,
        )
        if len(self._under_way) >= MAX_CALLS_UNDER_WAY:
            self._under_way.pop(next(iter(self._under_way)))
        self._under_way[run_id] = (agent, model, span)

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any = None, **kwargs) -> None:
        self._begin(run_id, kwargs)

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any = None, **kwargs) -> None:
        self._begin(run_id, kwargs)

    def on_llm_error(self, error: BaseException, *, run_id: Any = None, **kwargs) -> None:
        agent, model, span = self._under_way.pop(run_id, ("unmetered", "unknown", None))
        metrics.llm_call(agent, model, ok=False)
        if span is not None:
            traces.finish(span, error=error)

    def on_llm_end(self, response: LLMResult, *, run_id: Any = None, **kwargs) -> None:
        agent, asked_for, span = self._under_way.pop(run_id, ("unmetered", "unknown", None))
        model, tokens_in, tokens_out = None, 0, 0
        try:
            message = response.generations[0][0].message  # a chat model's reply
        except (AttributeError, IndexError):
            message = None
        counted = getattr(message, "usage_metadata", None) if message is not None else None
        if counted:  # a provider that said nothing: no row should claim tokens it cannot show
            named = (getattr(message, "response_metadata", None) or {}).get("model_name")
            model = named or (response.llm_output or {}).get("model_name") or "unknown"
            tokens_in, tokens_out = int(counted.get("input_tokens") or 0), int(counted.get("output_tokens") or 0)
            meter = _meter.get()
            if meter is not None:
                meter.add(model, tokens_in, tokens_out)

        price = price_of(model) if model else None
        cost = (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000 if price else None
        metrics.llm_call(agent, asked_for, ok=True, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
        if span is not None:
            traces.finish(
                span,
                {
                    "gen_ai.response.model": model,
                    "gen_ai.usage.input_tokens": tokens_in or None,
                    "gen_ai.usage.output_tokens": tokens_out or None,
                },
            )


# One recorder for the whole process. A ContextVar's default is seen in every context — every task,
# every thread — so LangChain's configure hook attaches the recorder to every model call, once.
_recorder: ContextVar[BaseCallbackHandler] = ContextVar("usage_recorder", default=_Recorder())
register_configure_hook(_recorder, inheritable=True)


@contextmanager
def metered(agent: str | None = None) -> Iterator[Usage]:
    """Count what the models are asked from here until the block ends. `agent`: whose work the block is."""
    usage = Usage(agent)
    token = _meter.set(usage)
    try:
        yield usage
    finally:
        try:
            _meter.reset(token)
        except ValueError:  # left in another context than it was entered in: nothing of ours to restore there
            pass
