/**
 * useSSE — React hook wrapping EventSource with reconnect logic.
 *
 * Browsers cannot set custom headers on EventSource, so the token is passed
 * as a ?token= query parameter — exactly what the backend's get_current_user_sse
 * dependency accepts (Phase 5).
 *
 * Reconnect strategy: exponential back-off (1s → 2s → 4s → 8s, cap 30s).
 * On reconnect we do NOT reset the events array so the UI shows the last
 * known agent status rather than a blank screen — roadmap requirement.
 * Redis pub/sub has no replay, so an event published while the stream was
 * down is gone; the trip page polls GET /status as the safety net.
 *
 * Phase 20: `builder_token` events carry the itinerary while it is written.
 * There can be dozens a second, so they never enter `events`; they are joined
 * into `draft`, which the page reads as text.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentStatus, SSEAgentUpdateEvent } from "./types";
import { getToken } from "./api";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];

export type SSEStatus = "idle" | "connecting" | "connected" | "reconnecting" | "closed";

/** The itinerary being written in the run in flight, as far as it has arrived. */
export interface BuilderDraft {
  /** The model's reply so far — JSON that is not finished (lib/draft.ts reads it). */
  text: string;
  /** 1 for the run's first build; more when a draft failed its checks and is being written again. */
  attempt: number;
}

export interface UseSSEResult {
  events: SSEAgentUpdateEvent[];
  status: SSEStatus;
  /** Null until the builder starts writing, and when its stream cannot be followed (see below). */
  draft: BuilderDraft | null;
  /** Forget the events of the previous run (the connection stays open). */
  reset: () => void;
}

export function useSSE(tripId: string | null, enabled: boolean = true): UseSSEResult {
  const [events, setEvents] = useState<SSEAgentUpdateEvent[]>([]);
  const [status, setStatus] = useState<SSEStatus>("idle");
  const [draft, setDraft] = useState<BuilderDraft | null>(null);

  const esRef      = useRef<EventSource | null>(null);
  const attempt    = useRef(0);
  const timerRef   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef  = useRef(false);   // set true when the caller calls close()

  // The token stream of the build in flight. Tokens are numbered from 0 within one build and only
  // make sense read from the start: after a gap (a dropped connection, a page opened mid-build)
  // the rest is ignored until the next build begins.
  const stream = useRef({ text: "", next: 0, builds: 0, readable: false });
  const frame  = useRef<number | null>(null);

  // Several tokens can arrive within one frame; the page is told once per frame.
  const showDraft = useCallback(() => {
    if (frame.current !== null) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      const { text, builds, readable } = stream.current;
      setDraft(readable ? { text, attempt: builds } : null);
    });
  }, []);

  const forgetDraft = useCallback(() => {
    stream.current = { text: "", next: 0, builds: 0, readable: false };
    showDraft();
  }, [showDraft]);

  const connect = useCallback(() => {
    if (!tripId || closedRef.current) return;

    const token = getToken();
    if (!token) return;

    setStatus(attempt.current === 0 ? "connecting" : "reconnecting");

    const url = `${API_BASE}/trips/${tripId}/stream?token=${encodeURIComponent(token)}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener("connected", () => {
      attempt.current = 0;
      setStatus("connected");
    });

    es.addEventListener("agent_update", (e: MessageEvent) => {
      let payload: SSEAgentUpdateEvent;
      try {
        payload = JSON.parse(e.data);
      } catch {
        return; // malformed JSON from server — ignore
      }

      if (payload.event === "builder_token") {
        const s = stream.current;
        if (payload.seq === 0) Object.assign(s, { text: "", next: 0, builds: s.builds + 1, readable: true });
        if (s.readable && payload.seq === s.next) {
          s.text += payload.token ?? "";
          s.next += 1;
        } else {
          s.readable = false;
        }
        showDraft();
        return;
      }

      if (payload.event === "planning_started") forgetDraft(); // a new run: whatever was being written is history
      setEvents((prev) => [...prev, payload]);
    });

    es.onerror = () => {
      es.close();
      esRef.current = null;
      if (closedRef.current) return;

      const delay = BACKOFF_MS[Math.min(attempt.current, BACKOFF_MS.length - 1)];
      attempt.current += 1;
      setStatus("reconnecting");

      timerRef.current = setTimeout(connect, delay);
    };
  }, [tripId, showDraft, forgetDraft]);

  // Open the connection when enabled & tripId changes
  useEffect(() => {
    if (!enabled || !tripId) return;

    closedRef.current = false;
    attempt.current   = 0;
    connect();

    return () => {
      closedRef.current = true;
      esRef.current?.close();
      esRef.current = null;
      if (timerRef.current) clearTimeout(timerRef.current);
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      frame.current = null;
      setStatus("closed");
    };
  }, [tripId, enabled, connect]);

  const reset = useCallback(() => {
    setEvents([]);
    forgetDraft();
  }, [forgetDraft]);

  return { events, status, draft, reset };
}

// ---------------------------------------------------------------------------
// Helpers consumed by AgentProgressPanel
// ---------------------------------------------------------------------------

export const AGENT_DISPLAY: Record<string, string> = {
  flight_agent:     "Flights",
  hotel_agent:      "Stay",
  activities_agent: "Things to do",
};

/** How each search is named in a sentence: "the flight search". */
export const SEARCH_WORDS: Record<string, string> = {
  flight_agent:     "flight",
  hotel_agent:      "hotel",
  activities_agent: "activities",
};

export type AgentState = "pending" | "running" | "completed" | "failed";

/**
 * What the latest run covers. A refinement repeats one search and carries the
 * others forward: `carried` holds the states of the searches it leaves alone,
 * and only events from index `since` on belong to it.
 */
export interface RunScope {
  since: number;
  carried: Record<string, AgentState>;
}

export const WHOLE_RUN: RunScope = { since: 0, carried: {} };

/** The latest result event each agent sent in this run (a carried agent keeps the one it had). */
export function latestAgentEvents(events: SSEAgentUpdateEvent[], scope: RunScope = WHOLE_RUN): Record<string, SSEAgentUpdateEvent> {
  const latest: Record<string, SSEAgentUpdateEvent> = {};
  events.forEach((ev, index) => {
    if (ev.agent && !ev.event && (index >= scope.since || scope.carried[ev.agent])) latest[ev.agent] = ev;
  });
  return latest;
}

/**
 * Per-agent badge state. SSE events are the live source; `polled` (from
 * GET /trips/{id}/status) fills in anything the stream missed, and an agent
 * with no result yet shows as running while a run is `active`.
 */
export function deriveAgentStates(
  events: SSEAgentUpdateEvent[],
  polled: Record<string, AgentStatus> = {},
  active: boolean = false,
  scope: RunScope = WHOLE_RUN,
): Record<string, AgentState> {
  const latest = latestAgentEvents(events, scope);
  const states: Record<string, AgentState> = {};
  for (const agent of Object.keys(AGENT_DISPLAY)) {
    const result = [latest[agent]?.status, polled[agent]].find((s) => s === "completed" || s === "failed");
    states[agent] = scope.carried[agent] ?? (result as AgentState | undefined) ?? (active ? "running" : "pending");
  }
  return states;
}

/**
 * Why each failed search failed, for the searches that are showing as failed. The live event says
 * it while the page is open; `polledErrors` (GET /status) says it after a reload. A search that
 * failed without giving a reason is listed with an empty one.
 */
export function agentFailures(
  events: SSEAgentUpdateEvent[],
  states: Record<string, AgentState>,
  polledErrors: Record<string, string> = {},
  scope: RunScope = WHOLE_RUN,
): Record<string, string> {
  const latest = latestAgentEvents(events, scope);
  const failures: Record<string, string> = {};
  for (const agent of Object.keys(AGENT_DISPLAY)) {
    if (states[agent] !== "failed") continue;
    failures[agent] = (latest[agent]?.status === "failed" ? latest[agent].summary : undefined) ?? polledErrors[agent] ?? "";
  }
  return failures;
}

/**
 * The failed searches a Retry cannot mend: the same search would fail the same way (a destination
 * no airport is known by, nothing within budget). The backend says which — src/ai/utils/failures.py —
 * in the live event while the page is open, in `polledRetryable` (GET /status) after a reload. A
 * failure it says nothing about may be retried.
 */
export function lastingFailures(
  events: SSEAgentUpdateEvent[],
  states: Record<string, AgentState>,
  polledRetryable: Record<string, boolean> = {},
  scope: RunScope = WHOLE_RUN,
): Set<string> {
  const latest = latestAgentEvents(events, scope);
  const lasting = new Set<string>();
  for (const agent of Object.keys(AGENT_DISPLAY)) {
    if (states[agent] !== "failed") continue;
    const live = latest[agent]?.status === "failed" ? latest[agent].retryable : undefined;
    if ((live ?? polledRetryable[agent]) === false) lasting.add(agent);
  }
  return lasting;
}
