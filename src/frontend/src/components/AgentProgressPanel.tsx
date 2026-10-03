"use client";

import { BedDouble, Check, CircleCheck, MapPin, NotebookPen, Plane, RotateCw, X, type LucideIcon } from "lucide-react";

import {
  AGENT_DISPLAY,
  SEARCH_WORDS,
  agentFailures,
  deriveAgentStates,
  lastingFailures,
  latestAgentEvents,
  type AgentState,
  type RunScope,
  type SSEStatus,
} from "@/lib/sse";
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
  /** Why searches failed, from GET /status: what a page loaded later knows about it. */
  polledErrors?: Record<string, string>;
  /** Whether running each failed search again could help, from GET /status. */
  polledRetryable?: Record<string, boolean>;
  /**
   * The fourth step, while a run is in flight: undefined until the builder starts writing, then
   * where it has got to ("Day 2 · Afternoon — Baga Beach"), or null before its first day.
   */
  writing?: string | null;
  /** Offer "Retry" beside a search that failed (Phase 20). */
  onRetry?: (agent: string) => void;
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

function StepIcon({ icon: Icon, quiet }: { icon: LucideIcon; quiet: boolean }) {
  return (
    <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg transition-colors ${quiet ? "bg-ink-50 text-ink-400" : "bg-ink-100 text-ink-700"}`}>
      <Icon className="h-4 w-4" aria-hidden />
    </span>
  );
}

/** The steps behind a plan — three searches, then the writing — updated live from the SSE stream. */
export default function AgentProgressPanel({
  events,
  sseStatus,
  active,
  polled,
  scope,
  polledErrors = {},
  polledRetryable = {},
  writing,
  onRetry,
}: Props) {
  const agentStates = deriveAgentStates(events, polled, active, scope);
  // what each agent last said ("Found 5 flights"), and why the failed ones failed
  const latest = latestAgentEvents(events, scope);
  const failures = agentFailures(events, agentStates, polledErrors, scope);
  // no Retry where it would fail the same way: the reason says what would help instead
  const lasting = lastingFailures(events, agentStates, polledRetryable, scope);

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
          const failed = state === "failed";
          const summary = failed ? failures[key] || IDLE_TEXT.failed : latest[key]?.summary ?? IDLE_TEXT[state];
          return (
            <li key={key} data-agent={key} data-state={state} className="flex items-start gap-3 py-1">
              <StepIcon icon={AGENT_ICONS[key] ?? MapPin} quiet={state === "pending"} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium leading-tight text-ink-900">{AGENT_DISPLAY[key]}</p>
                {/* why a search failed is worth reading in full; anything else fits on a line */}
                <p className={`text-xs ${failed ? "break-words text-bad-ink" : "truncate text-ink-500"}`} title={failed ? undefined : summary}>
                  {summary}
                </p>
                {failed && onRetry && !active && !lasting.has(key) && (
                  <button
                    onClick={() => onRetry(key)}
                    aria-label={`Retry the ${SEARCH_WORDS[key]} search`}
                    className="btn-secondary mt-1.5 gap-1.5 rounded-lg px-2.5 py-1 text-xs"
                  >
                    <RotateCw className="h-3 w-3" aria-hidden />
                    Retry
                  </button>
                )}
              </div>
              <span className="mt-1.5">
                <StateMark state={state} />
              </span>
            </li>
          );
        })}

        {active && (
          <li data-agent="itinerary_builder" data-state={writing === undefined ? "pending" : "running"} className="flex items-start gap-3 py-1">
            <StepIcon icon={NotebookPen} quiet={writing === undefined} />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium leading-tight text-ink-900">Itinerary</p>
              <p className="truncate text-xs text-ink-500">
                {writing === undefined ? "Written once the searches are in" : writing ? `Writing · ${writing}` : "Writing…"}
              </p>
            </div>
            <span className="mt-1.5">
              <StateMark state={writing === undefined ? "pending" : "running"} />
            </span>
          </li>
        )}
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
