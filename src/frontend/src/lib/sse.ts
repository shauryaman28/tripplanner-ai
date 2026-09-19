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
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { SSEAgentUpdateEvent } from "./types";
import { getToken } from "./api";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];

export type SSEStatus = "idle" | "connecting" | "connected" | "reconnecting" | "closed";

export interface UseSSEResult {
  events: SSEAgentUpdateEvent[];
  status: SSEStatus;
  /** Manually close the SSE connection and stop reconnecting. */
  close: () => void;
}

export function useSSE(tripId: string | null, enabled: boolean = true): UseSSEResult {
  const [events, setEvents] = useState<SSEAgentUpdateEvent[]>([]);
  const [status, setStatus] = useState<SSEStatus>("idle");

  const esRef      = useRef<EventSource | null>(null);
  const attempt    = useRef(0);
  const timerRef   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef  = useRef(false);   // set true when the caller calls close()

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
      try {
        const payload: SSEAgentUpdateEvent = JSON.parse(e.data);
        setEvents((prev) => [...prev, payload]);
      } catch {
        // malformed JSON from server — ignore
      }
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
  }, [tripId]);

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
      setStatus("closed");
    };
  }, [tripId, enabled, connect]);

  const close = useCallback(() => {
    closedRef.current = true;
    esRef.current?.close();
    esRef.current = null;
    if (timerRef.current) clearTimeout(timerRef.current);
    setStatus("closed");
  }, []);

  return { events, status, close };
}

// ---------------------------------------------------------------------------
// Helpers consumed by AgentProgressPanel
// ---------------------------------------------------------------------------

export const AGENT_DISPLAY: Record<string, string> = {
  flight_agent:     "Flights",
  hotel_agent:      "Hotels",
  activities_agent: "Activities",
};

export function deriveAgentStates(
  events: SSEAgentUpdateEvent[],
): Record<string, "pending" | "running" | "completed" | "failed"> {
  const states: Record<string, "pending" | "running" | "completed" | "failed"> = {
    flight_agent:     "pending",
    hotel_agent:      "pending",
    activities_agent: "pending",
  };

  for (const ev of events) {
    if (ev.event === "planning_started") {
      // Mark all as running once planning kicks off
      Object.keys(states).forEach((k) => {
        if (states[k] === "pending") states[k] = "running";
      });
    }
    if (ev.agent && ev.agent in states && ev.status) {
      states[ev.agent] = ev.status as typeof states[keyof typeof states];
    }
  }

  return states;
}
