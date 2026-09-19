"use client";

import type { SSEAgentUpdateEvent } from "@/lib/types";
import { AGENT_DISPLAY, deriveAgentStates, type SSEStatus } from "@/lib/sse";

interface Props {
  events: SSEAgentUpdateEvent[];
  sseStatus: SSEStatus;
}

type AgentState = "pending" | "running" | "completed" | "failed";

function StatusIcon({ state }: { state: AgentState }) {
  if (state === "completed") {
    return (
      <svg className="h-4 w-4 text-green-500" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z" />
      </svg>
    );
  }
  if (state === "failed") {
    return (
      <svg className="h-4 w-4 text-red-500" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" />
      </svg>
    );
  }
  if (state === "running") {
    return (
      <span className="flex h-4 w-4 items-center justify-center">
        <span className="h-2 w-2 rounded-full bg-brand-500 animate-pulse-dot" />
      </span>
    );
  }
  // pending
  return (
    <span className="h-4 w-4 rounded-full border-2 border-gray-200 flex-shrink-0" />
  );
}

function ConnectionBadge({ status }: { status: SSEStatus }) {
  const labels: Record<SSEStatus, string> = {
    idle:         "Idle",
    connecting:   "Connecting…",
    connected:    "Live",
    reconnecting: "Reconnecting…",
    closed:       "Disconnected",
  };
  const colors: Record<SSEStatus, string> = {
    idle:         "bg-gray-100 text-gray-500",
    connecting:   "bg-yellow-50 text-yellow-600",
    connected:    "bg-green-50 text-green-700",
    reconnecting: "bg-orange-50 text-orange-600",
    closed:       "bg-gray-100 text-gray-500",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colors[status]}`}>
      {labels[status]}
    </span>
  );
}

export default function AgentProgressPanel({ events, sseStatus }: Props) {
  const agentStates = deriveAgentStates(events);

  // Extract the latest summary per agent from events
  const summaries: Record<string, string> = {};
  for (const ev of events) {
    if (ev.agent && ev.summary) summaries[ev.agent] = ev.summary;
  }

  // Budget conflict
  const conflictEvent = [...events].reverse().find((e) => e.event === "budget_conflict");
  const isComplete    = events.some((e) => e.event === "planning_complete");

  const agentKeys = Object.keys(AGENT_DISPLAY) as Array<keyof typeof AGENT_DISPLAY>;

  return (
    <aside
      aria-label="Planning progress"
      className="card p-4 space-y-4"
    >
      <div className="flex items-center justify-between">
        <h2 className="font-display text-sm font-semibold text-gray-900">Planning progress</h2>
        <ConnectionBadge status={sseStatus} />
      </div>

      <ul className="space-y-3">
        {agentKeys.map((key) => {
          const state = agentStates[key] as AgentState;
          const label = AGENT_DISPLAY[key];
          const summary = summaries[key];

          return (
            <li key={key} className="flex items-start gap-3">
              <StatusIcon state={state} />
              <div className="min-w-0">
                <p className="text-sm font-medium text-gray-800">{label}</p>
                {summary && (
                  <p className="text-xs text-muted truncate">{summary}</p>
                )}
                {!summary && state === "running" && (
                  <p className="text-xs text-muted">Searching…</p>
                )}
              </div>
            </li>
          );
        })}
      </ul>

      {/* Budget conflict notice */}
      {conflictEvent?.reason && !isComplete && (
        <div className="rounded-lg bg-orange-50 border border-orange-100 p-3">
          <p className="text-xs font-medium text-orange-700 mb-1">Budget conflict</p>
          <p className="text-xs text-orange-600">{conflictEvent.reason}</p>
          {conflictEvent.options && conflictEvent.options.length > 0 && (
            <ul className="mt-2 space-y-1">
              {conflictEvent.options.map((opt) => (
                <li key={opt.choice} className="text-xs text-orange-700">
                  · {opt.description} ({opt.estimated_saving})
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Complete banner */}
      {isComplete && (
        <div className="rounded-lg bg-green-50 border border-green-100 px-3 py-2">
          <p className="text-xs font-medium text-green-700">Planning complete ✓</p>
        </div>
      )}
    </aside>
  );
}
