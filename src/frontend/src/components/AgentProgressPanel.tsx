"use client";

import { BedDouble, Check, CircleCheck, MapPin, Plane, X, type LucideIcon } from "lucide-react";

import { AGENT_DISPLAY, deriveAgentStates, latestAgentEvents, type AgentState, type RunScope, type SSEStatus } from "@/lib/sse";
import type { AgentStatus, SSEAgentUpdateEvent } from "@/lib/types";
import { Spinner } from "./ui";

interface Props {
  events: SSEAgentUpdateEvent[];
  sseStatus: SSEStatus;
  /** A planning run is in flight. */
  active: boolean;
  /** Agent statuses from the polling fallback (GET /trips/{id}/status). */
  polled: Record<string, AgentStatus>;
  /** Which searches the latest run repeats — a refinement leaves the others as they were. */
  scope: RunScope;
}

const AGENT_ICONS: Record<string, LucideIcon> = {
  flight_agent:     Plane,
  hotel_agent:      BedDouble,
  activities_agent: MapPin,
};

const IDLE_TEXT: Record<AgentState, string> = {
  pending:   "Not started",
  running:   "Searching…",
  completed: "Done",
  failed:    "Could not search",
};

function StateMark({ state }: { state: AgentState }) {
  if (state === "running") return <Spinner className="h-4 w-4 text-ink-500" />;
  if (state === "completed") {
    return (
      <span className="grid h-5 w-5 place-items-center rounded-full bg-good text-white">
        <Check className="h-3 w-3" strokeWidth={3} aria-hidden />
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className="grid h-5 w-5 place-items-center rounded-full bg-bad text-white">
        <X className="h-3 w-3" strokeWidth={3} aria-hidden />
      </span>
    );
  }
  return <span className="h-5 w-5 rounded-full border-2 border-dashed border-ink-200" />;
}

function ConnectionBadge({ status }: { status: SSEStatus }) {
  const labels: Record<SSEStatus, string> = {
    idle:         "Idle",
    connecting:   "Connecting…",
    connected:    "Live",
    reconnecting: "Reconnecting…",
    closed:       "Disconnected",
  };
  const live = status === "connected";
  const retrying = status === "connecting" || status === "reconnecting";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ${
        live ? "bg-good-soft text-good-ink" : retrying ? "bg-warn-soft text-warn-ink" : "bg-ink-100 text-ink-500"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${live ? "bg-good" : retrying ? "animate-pulse bg-warn" : "bg-ink-300"}`} aria-hidden />
      {labels[status]}
    </span>
  );
}

/** The three searches behind a plan, updated live from the SSE stream. */
export default function AgentProgressPanel({ events, sseStatus, active, polled, scope }: Props) {
  const agentStates = deriveAgentStates(events, polled, active, scope);
  // what each agent last said ("Found 5 flights", or why it failed)
  const latest = latestAgentEvents(events, scope);

  // Events span every run on this page: the run is complete if nothing has started since.
  const lifecycle = events.filter((e) => e.event === "planning_started" || e.event === "planning_complete");
  const isComplete = !active && lifecycle[lifecycle.length - 1]?.event === "planning_complete";

  return (
    <section aria-label="Planning progress">
      <div className="flex items-center justify-between">
        <h2 className="eyebrow">Planning progress</h2>
        <ConnectionBadge status={sseStatus} />
      </div>

      <ul className="mt-2.5 space-y-1">
        {Object.keys(AGENT_DISPLAY).map((key) => {
          const state: AgentState = agentStates[key];
          const summary = latest[key]?.summary;
          const Icon = AGENT_ICONS[key] ?? MapPin;
          return (
            <li key={key} className="flex items-center gap-3 py-1">
              <span
                className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg transition-colors ${
                  state === "pending" ? "bg-ink-50 text-ink-400" : "bg-ink-100 text-ink-700"
                }`}
              >
                <Icon className="h-4 w-4" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium leading-tight text-ink-900">{AGENT_DISPLAY[key]}</p>
                <p className="truncate text-xs text-ink-500" title={summary}>
                  {summary ?? IDLE_TEXT[state]}
                </p>
              </div>
              <StateMark state={state} />
            </li>
          );
        })}
      </ul>

      {isComplete && (
        <p className="mt-2 flex items-center gap-1.5 text-xs font-medium text-good-ink">
          <CircleCheck className="h-3.5 w-3.5" aria-hidden />
          Planning complete
        </p>
      )}
    </section>
  );
}
