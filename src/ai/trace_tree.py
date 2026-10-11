"""Runs as LangSmith is sent them, put back together as the trees they are (Phase 26).

LangSmith's library uploads a trace as a stream of flat records — a run when it
starts, the same run again when it ends, each pointing at its parent. This
module is the other end of that, as far as this project needs one: it merges
the records, hangs every run under its parent, and can say what a trace took
and what it asked of the models.

Two things use it. `scripts/trace_receiver.py` is a local stand-in for LangSmith
— it prints each trace it is sent, for developing without an account. And the
tests read what the planner *would upload* with it, which is the one honest
way to assert what a trace looks like: the records LangSmith receives are the
trace, whatever the code that made them intended.

Nothing here talks to a network, and nothing here is used when the planner runs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# What the tracing library asks a server before it sends anything (GET /info), answered as a server
# that takes plain JSON batches. Every key is needed: a batch config the library cannot read stops
# its sending thread, and with it every trace — silently.
SERVER_INFO = {
    "version": "local-stand-in",
    "batch_ingest_config": {
        "use_multipart_endpoint": False,  # POST /runs/batch, as JSON
        "scale_up_qsize_trigger": 1000,
        "scale_up_nthreads_limit": 16,
        "scale_down_nempty_trigger": 4,
        "size_limit": 100,
        "size_limit_bytes": 20_971_520,
    },
}


@dataclass
class Span:
    """One run of a trace, with the runs made inside it."""

    id: str
    name: str
    run_type: str  # "chain" | "llm" | "tool" | …
    parent_id: str | None
    started: datetime | None
    ended: datetime | None
    error: str | None
    own_tokens: int  # what this run itself asked of a model: only a model call has any
    metadata: dict[str, Any]
    tags: list[str]
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    project: str | None
    children: list[Span] = field(default_factory=list)

    @property
    def duration_ms(self) -> int | None:
        if self.started is None or self.ended is None:
            return None
        return int((self.ended - self.started).total_seconds() * 1000)

    @property
    def tokens(self) -> int:
        """This run's tokens and those of every run inside it."""
        return self.own_tokens + sum(child.tokens for child in self.children)

    def walk(self):
        """This span and every span beneath it, parents first, in the order they started."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, name: str) -> list[Span]:
        """Every span of this name in the trace."""
        return [span for span in self.walk() if span.name == name]

    def names(self) -> list[str]:
        """The names of the spans directly inside this one."""
        return [child.name for child in self.children]


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _model_tokens(run: dict) -> int:
    """The tokens a model call reports: its reply's usage, as LangChain uploads it."""
    if run.get("run_type") != "llm":
        return 0
    outputs = run.get("outputs") or {}
    try:
        message = outputs["generations"][0][0]["message"]
        usage = (message.get("kwargs") or message).get("usage_metadata") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        usage = {}
    if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    counted = (outputs.get("llm_output") or {}).get("token_usage") or {}
    return int(counted.get("total_tokens") or 0) if isinstance(counted, dict) else 0


class Runs:
    """Every run record received, merged by run. Safe to add to from several threads."""

    def __init__(self) -> None:
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def take(self, batch: dict) -> None:
        """A batch as POST /runs/batch carries it: {"post": [runs started], "patch": [runs ended]}."""
        for kind in ("post", "patch"):
            for run in batch.get(kind) or []:
                self.take_one(run)

    def take_one(self, run: dict) -> None:
        """One record. A later record of the same run adds to the earlier one — never takes anything away."""
        run_id = str(run.get("id") or "")
        if not run_id:
            return
        with self._lock:
            kept = self._runs.setdefault(run_id, {})
            kept.update({key: value for key, value in run.items() if value is not None})

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)

    def records(self) -> list[dict]:
        """Every run as it was received, merged — all of it: inputs, outputs, the model's settings."""
        with self._lock:
            return [dict(run) for run in self._runs.values()]

    def traces(self, trip_id: str | None = None) -> list[Span]:
        """The traces received, oldest first — or only those of one trip.

        A trace belongs to a trip when its root carries that `trip_id` as
        metadata: what LangSmith's filter on metadata does.
        """
        with self._lock:
            records = [dict(run) for run in self._runs.values()]

        spans: dict[str, Span] = {}
        order: dict[str, str] = {}
        for run in records:
            extra = run.get("extra") or {}
            span = Span(
                id=str(run["id"]),
                name=str(run.get("name") or "?"),
                run_type=str(run.get("run_type") or "chain"),
                parent_id=str(run["parent_run_id"]) if run.get("parent_run_id") else None,
                started=_moment(run.get("start_time")),
                ended=_moment(run.get("end_time")),
                error=run.get("error"),
                own_tokens=_model_tokens(run),
                metadata=dict(extra.get("metadata") or {}),
                tags=list(run.get("tags") or []),
                inputs=dict(run.get("inputs") or {}),
                outputs=dict(run.get("outputs") or {}),
                project=run.get("session_name"),
            )
            spans[span.id] = span
            order[span.id] = str(run.get("dotted_order") or run.get("start_time") or "")

        roots: list[Span] = []
        for span in sorted(spans.values(), key=lambda s: order[s.id]):
            parent = spans.get(span.parent_id) if span.parent_id else None
            (parent.children if parent else roots).append(span)
        if trip_id is not None:
            roots = [root for root in roots if str(root.metadata.get("trip_id")) == str(trip_id)]
        return roots


def render(root: Span) -> str:
    """A trace as text: one line a span, indented under its parent, with how long it took and its tokens.

    plan trip   trip 1fee8655  user 3a2c91b0  turn 1                 9.9 s   5,412 tokens
    ├─ intent_parsing                                                1.4 s     312 tokens
    │  └─ ChatGoogleGenerativeAI  [llm]                              1.3 s     312 tokens
    ├─ run_flight                                                    0.1 s
    …
    """
    lines: list[str] = []

    def took(span: Span) -> str:
        if span.duration_ms is None:
            return "   …    "
        return f"{span.duration_ms / 1000:6.1f} s"

    def line(span: Span, prefix: str, label: str) -> None:
        kind = "" if span.run_type == "chain" else f"  [{span.run_type}]"
        failed = "  ✗ " + str(span.error).splitlines()[0][:60] if span.error else ""
        tokens = f"{span.tokens:>8,} tokens" if span.tokens else ""
        lines.append(f"{(prefix + label + kind):<68}{took(span)} {tokens}{failed}".rstrip())

    def below(span: Span, prefix: str) -> None:
        for place, child in enumerate(span.children):
            last = place == len(span.children) - 1
            line(child, prefix + ("└─ " if last else "├─ "), child.name)
            below(child, prefix + ("   " if last else "│  "))

    about = (
        "  ".join(f"{key} {str(root.metadata[key])[:8]}" for key in ("trip_id", "user_id") if root.metadata.get(key))
        .replace("trip_id", "trip")
        .replace("user_id", "user")
    )
    if root.metadata.get("turn") is not None:
        about += f"  turn {root.metadata['turn']}"
    line(root, "", f"{root.name}   {about}".rstrip())
    below(root, "")
    return "\n".join(lines)
