"use client";

/**
 * What the plan column of the trip page shows when there is no itinerary to show:
 * nothing planned yet, a question to answer, a run in flight, a run that failed.
 */

import {
  BedDouble,
  CalendarDays,
  CalendarMinus,
  CalendarRange,
  CircleAlert,
  CircleCheck,
  MapPin,
  MessageCircleQuestion,
  Plane,
  RotateCw,
  TriangleAlert,
  Users,
  Wallet,
  type LucideIcon,
} from "lucide-react";

import { formatDateRange, formatINR, nightsBetween, plural } from "@/lib/format";
import { AGENT_DISPLAY, type AgentState, type BuilderDraft } from "@/lib/sse";
import type { BudgetConflict, BudgetConflictOption, ReplanChoice, Trip, TripStatus } from "@/lib/types";
import LiveDraft from "./LiveDraft";
import { AssistantAvatar } from "./MessageThread";
import { TripStatusBadge } from "./ui";

export type PlanningPhase =
  | "loading"       // fetching the trip
  | "idle"          // trip exists, nothing started
  | "planning"      // agents running
  | "clarifying"    // backend returned clarification_needed
  | "complete"      // itinerary ready
  | "failed"        // last run failed and there is no itinerary to show
  | "refining";     // a change is being made (the current itinerary stays visible)

// One tap instead of typing — each is sent to the assistant as written.
export const STARTERS = [
  "A relaxed trip with beaches and good food",
  "Pack in history, culture and sightseeing",
  "Keep it budget-friendly and close to nature",
];

const PHASE_BADGE: Record<Exclude<PlanningPhase, "loading">, { status: TripStatus; label?: string }> = {
  idle:       { status: "pending" },
  planning:   { status: "planning" },
  refining:   { status: "planning", label: "Updating…" },
  clarifying: { status: "pending", label: "Needs your answer" },
  complete:   { status: "completed" },
  failed:     { status: "failed" },
};

const REPLAN_ICONS: Record<ReplanChoice, LucideIcon> = {
  cheaper_flights: Plane,
  reduce_days:     CalendarMinus,
  increase_budget: Wallet,
  cheaper_hotel:   BedDouble,
  off_peak:        CalendarRange,
};

// The ways out that change the trip itself, priced (Phase 21); the others are plain actions.
const PRICED_CHOICES = new Set<ReplanChoice>(["cheaper_hotel", "reduce_days", "off_peak"]);

/** One way to bring the trip within the budget: what changes, what it would come to, and whether that fits. */
function AlternativeCard({
  option,
  budget,
  onPick,
  disabled,
}: {
  option: BudgetConflictOption;
  budget: number | null;
  onPick: () => void;
  disabled: boolean;
}) {
  const Icon = REPLAN_ICONS[option.choice] ?? Wallet;
  const priced = typeof option.total === "number";
  const over = priced && budget !== null ? (option.total as number) - budget : null;
  const saving = option.flight_saving
    ? `Flights about ${formatINR(option.flight_saving)} less`
    : option.saving
      ? `About ${formatINR(option.saving)} less than as asked`
      : null;
  return (
    <button
      onClick={onPick}
      disabled={disabled}
      data-alternative={option.choice}
      className="focus-ring group flex flex-col rounded-2xl border border-ink-200 bg-white p-4 text-left transition
                 hover:-translate-y-0.5 hover:border-ink-900 hover:shadow-lift disabled:pointer-events-none disabled:opacity-50"
    >
      <span className="grid h-9 w-9 place-items-center rounded-xl bg-ink-100 text-ink-700 transition-colors group-hover:bg-ink-900 group-hover:text-white">
        <Icon className="h-[18px] w-[18px]" aria-hidden />
      </span>
      <p className="mt-3 text-sm font-medium leading-snug text-ink-900">{option.description}</p>
      {priced ? (
        <>
          <p className="mt-2 text-lg font-semibold tabular-nums tracking-tight text-ink-900">About {formatINR(option.total as number)}</p>
          {/* a status is said in words, not by its colour alone */}
          {over !== null && (
            <p className={`mt-0.5 inline-flex items-center gap-1 text-xs font-medium ${over <= 0 ? "text-good-ink" : "text-warn-ink"}`}>
              {over <= 0 ? <CircleCheck className="h-3.5 w-3.5" aria-hidden /> : <TriangleAlert className="h-3.5 w-3.5" aria-hidden />}
              {over <= 0 ? "Within your budget" : `${formatINR(over)} over budget`}
            </p>
          )}
          {saving && <p className="mt-1 text-xs text-ink-500">{saving}</p>}
        </>
      ) : (
        <p className="mt-1 text-xs text-ink-500">{option.estimated_saving}</p>
      )}
    </button>
  );
}

const SEARCH_ICONS: Record<string, LucideIcon> = {
  flight_agent:     Plane,
  hotel_agent:      BedDouble,
  activities_agent: MapPin,
};

export function TripHero({ trip, phase }: { trip: Trip | null; phase: PlanningPhase }) {
  if (!trip || phase === "loading") {
    return (
      <div aria-hidden>
        <div className="skeleton h-12 w-64 max-w-full" />
        <div className="skeleton mt-4 h-5 w-96 max-w-full" />
      </div>
    );
  }

  const nights = nightsBetween(trip.start_date, trip.end_date);
  return (
    <header className="animate-fade">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        {/* a destination can be one long word; it breaks rather than push the page wider than the screen */}
        <h1 className="min-w-0 text-balance break-words font-display text-4xl font-medium leading-[1.05] tracking-tight text-ink-900 sm:text-5xl">
          {trip.destination}
        </h1>
        <div className="pt-2">
          <TripStatusBadge {...PHASE_BADGE[phase]} />
        </div>
      </div>

      <ul className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-sm text-ink-600">
        <li className="flex items-center gap-2">
          <CalendarDays className="h-4 w-4 text-ink-400" aria-hidden />
          {formatDateRange(trip.start_date, trip.end_date)} · {plural(nights, "night")}
        </li>
        <li className="flex items-center gap-2">
          <Users className="h-4 w-4 text-ink-400" aria-hidden />
          {plural(trip.group_size, "traveller")}
        </li>
        <li className="flex items-center gap-2">
          <Wallet className="h-4 w-4 text-ink-400" aria-hidden />
          {formatINR(trip.budget)} budget
        </li>
      </ul>

      {trip.interests && trip.interests.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-1.5" aria-label="Interests">
          {trip.interests.map((interest) => (
            <li key={interest} className="chip max-w-full break-words">
              {interest}
            </li>
          ))}
        </ul>
      )}
    </header>
  );
}

/** One-tap messages. `compact` is the size that sits above the composer. */
export function SuggestionChips({
  ideas,
  onPick,
  disabled,
  compact = false,
  label,
}: {
  ideas: string[];
  onPick: (text: string) => void;
  disabled?: boolean;
  compact?: boolean;
  label?: string;
}) {
  return (
    <div className={`flex flex-wrap ${compact ? "gap-1.5" : "gap-2"}`} role={label ? "group" : undefined} aria-label={label}>
      {ideas.map((idea) => (
        <button
          key={idea}
          onClick={() => onPick(idea)}
          disabled={disabled}
          className={`focus-ring rounded-full border border-ink-200 bg-white text-left transition-colors hover:border-ink-900 hover:text-ink-900
                      disabled:pointer-events-none disabled:opacity-50 ${
                        compact ? "px-3 py-1 text-xs font-medium text-ink-600" : "px-3.5 py-1.5 text-sm text-ink-700"
                      }`}
        >
          {idea}
        </button>
      ))}
    </div>
  );
}

/** Nothing planned yet: say what to do next. */
export function StartStage({ destination, onPick }: { destination: string; onPick: (text: string) => void }) {
  return (
    <div className="card relative animate-rise overflow-hidden p-7 sm:p-10">
      <div className="pointer-events-none absolute -right-16 -top-20 h-64 w-64 rounded-full bg-sunset opacity-20 blur-3xl" aria-hidden />
      <AssistantAvatar size="h-11 w-11" />
      <h2 className="mt-5 text-balance break-words font-display text-3xl font-medium tracking-tight text-ink-900">Let&apos;s plan {destination}</h2>
      <p className="mt-2 max-w-prose text-ink-600">
        Tell the assistant what you want from this trip — the pace, what you enjoy, where you&apos;re flying from. It finds
        flights, a place to stay and things to do, and keeps the plan inside your budget.
      </p>
      <p className="eyebrow mt-8">Or start from one of these</p>
      <div className="mt-3">
        <SuggestionChips ideas={STARTERS} onPick={onPick} />
      </div>
    </div>
  );
}

export function ClarifyStage({ question }: { question: string | null }) {
  return (
    <div className="card animate-rise p-7 sm:p-10">
      <span className="grid h-11 w-11 place-items-center rounded-full bg-ink-100 text-ink-700">
        <MessageCircleQuestion className="h-5 w-5" aria-hidden />
      </span>
      <h2 className="mt-5 font-display text-3xl font-medium tracking-tight text-ink-900">One quick question</h2>
      <p className="mt-2 max-w-prose text-ink-600">{question ?? "The assistant needs a little more detail."}</p>
      <p className="mt-4 text-sm text-ink-500">Answer in the assistant panel and planning starts straight away.</p>
    </div>
  );
}

/** A run is in flight and there is no itinerary to show yet. Once the builder writes, its draft is the stage. */
export function PlanningStage({
  destination,
  agents,
  draft,
}: {
  destination: string;
  agents: Record<string, AgentState>;
  draft: BuilderDraft | null;
}) {
  const step = draft
    ? "Writing the day-by-day plan…"
    : agents.flight_agent === "running"
    ? "Searching flights…"
    : agents.hotel_agent === "running" || agents.activities_agent === "running"
    ? "Finding a place to stay and things to do…"
    : "Writing the day-by-day plan and checking it…";

  return (
    <div className="space-y-4" aria-busy="true">
      <div className="card animate-rise p-6 sm:p-7">
        <div className="flex items-center gap-4">
          <AssistantAvatar size="h-11 w-11" />
          <div className="min-w-0">
            <h2 className="break-words font-display text-2xl font-medium tracking-tight text-ink-900">Planning {destination}…</h2>
            <p className="mt-0.5 text-sm text-ink-600" aria-live="polite">
              {step}
            </p>
          </div>
        </div>
        <div className="mt-6 h-1.5 overflow-hidden rounded bg-ink-100" aria-hidden>
          <div className="h-full w-2/5 animate-indeterminate rounded bg-ink-900" />
        </div>
      </div>

      {draft ? (
        <LiveDraft draft={draft} />
      ) : (
        // stand-ins for the plan — on a small screen they would only push the live progress further down
        <>
          <div className="skeleton hidden h-44 lg:block" aria-hidden />
          <div className="skeleton hidden h-44 lg:block" aria-hidden />
        </>
      )}
    </div>
  );
}

/** The last run failed and nothing can be shown: say what went wrong, and offer the way forward. */
export function FailedStage({
  conflict,
  reason,
  failures,
  onReplan,
  onRetry,
  onPick,
  disabled,
}: {
  conflict: BudgetConflict | null;
  /** Why the run failed, in the planner's words; null when it could not say. */
  reason: string | null;
  /** The searches that failed, each with why (possibly ""). */
  failures: Record<string, string>;
  onReplan: (option: BudgetConflictOption) => void;
  /** Plan the trip again as it is. */
  onRetry: () => void;
  onPick: (text: string) => void;
  disabled: boolean;
}) {
  if (conflict) {
    const estimate = conflict.estimate ?? null;
    const alternatives = conflict.options.filter((option) => PRICED_CHOICES.has(option.choice));
    const actions = conflict.options.filter((option) => !PRICED_CHOICES.has(option.choice));
    return (
      <div className="card animate-rise p-7 sm:p-10" aria-label="Budget options">
        <p className="inline-flex items-center gap-1.5 rounded-full bg-warn-soft px-3 py-1.5 text-xs font-medium text-warn-ink">
          <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
          Over budget
        </p>
        <h2 className="mt-4 text-balance font-display text-3xl font-medium tracking-tight text-ink-900">
          The flights don&apos;t leave enough for the rest
        </h2>
        <p className="mt-2 max-w-prose text-ink-600">{conflict.reason}</p>
        {estimate && (
          <p className="mt-3 max-w-prose text-sm text-ink-600" data-estimate>
            With {estimate.stay} and things to do, the trip as asked comes to about{" "}
            <span className="font-semibold text-ink-900">{formatINR(estimate.total)}</span> — likely between{" "}
            {formatINR(estimate.total_min)} and {formatINR(estimate.total_max)} once booked. {estimate.about}
          </p>
        )}

        {alternatives.length > 0 && (
          <>
            <p className="eyebrow mt-7">Ways to bring it within {estimate ? formatINR(estimate.budget) : "the budget"}</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              {alternatives.map((option) => (
                <AlternativeCard
                  key={option.choice}
                  option={option}
                  budget={estimate?.budget ?? null}
                  onPick={() => onReplan(option)}
                  disabled={disabled}
                />
              ))}
            </div>
          </>
        )}
        {/* the plain actions; a conflict recorded before Phase 21 carries no amounts, and its cards show its words */}
        {actions.length > 0 && (
          <div className={`flex flex-wrap gap-2 ${alternatives.length > 0 ? "mt-4" : "mt-7"}`}>
            {actions.map((option) => {
              const Icon = REPLAN_ICONS[option.choice] ?? Wallet;
              return (
                <button
                  key={option.choice}
                  onClick={() => onReplan(option)}
                  disabled={disabled}
                  title={option.estimated_saving}
                  className="btn-secondary gap-2 rounded-xl px-3.5 py-2 text-sm"
                >
                  <Icon className="h-4 w-4" aria-hidden />
                  {option.description}
                </button>
              );
            })}
          </div>
        )}
        <p className="mt-5 text-sm text-ink-500">
          {conflict.options.length > 0 ? "Or describe" : "Describe"} a different trip to the assistant.
        </p>
      </div>
    );
  }

  const failed = Object.keys(failures);
  return (
    <div className="card animate-rise p-7 sm:p-10" aria-label="Planning failed">
      <span className="grid h-11 w-11 place-items-center rounded-full bg-bad-soft text-bad-ink">
        <CircleAlert className="h-5 w-5" aria-hidden />
      </span>
      <h2 className="mt-5 font-display text-3xl font-medium tracking-tight text-ink-900">That plan didn&apos;t come together</h2>
      <p className="mt-2 max-w-prose break-words text-ink-600">{reason ?? "Something went wrong along the way."} Nothing was saved.</p>

      {/* which search failed, and why — the reason above is the run's, these are each search's own */}
      {failed.length > 0 && (
        <ul className="mt-5 divide-y divide-ink-100 rounded-xl border border-ink-200/70" aria-label="Searches that failed">
          {failed.map((agent) => {
            const Icon = SEARCH_ICONS[agent] ?? MapPin;
            return (
              <li key={agent} className="flex items-start gap-3 px-3.5 py-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-bad-soft text-bad-ink">
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink-900">{AGENT_DISPLAY[agent] ?? agent}</p>
                  <p className="break-words text-xs text-ink-600">{failures[agent] || "The search could not be run."}</p>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <button onClick={onRetry} disabled={disabled} className="btn-primary mt-6">
        <RotateCw className="h-4 w-4" aria-hidden />
        Retry
      </button>

      <p className="eyebrow mt-8">Or describe it differently</p>
      <div className="mt-3">
        <SuggestionChips ideas={STARTERS} onPick={onPick} disabled={disabled} />
      </div>
    </div>
  );
}
