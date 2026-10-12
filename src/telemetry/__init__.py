"""What the running system says about itself (Phase 29): metrics, traces, and log lines that can be searched.

    context.py   which trip, and whose, the work under way is for — two context variables
    metrics.py   Prometheus: the counters, gauges and histograms that are this app's own
    traces.py    OpenTelemetry: one trace from the request that asked to the row that was written
    logs.py      every log line a JSON object carrying trip_id, user_id and the trace it belongs to

Two processes use this package — the API server and the tool server it starts (src/ai/mcp_server) —
and nothing in it imports either of them: each hands its own settings in.

Not to be confused with src/ai/tracing.py (Phase 26). That one uploads what the planner *thought* —
prompts, replies, the state between steps — to LangSmith. This one records what the system *did*:
which requests, queries, commands and calls, how long each took, and which of them failed.
"""
