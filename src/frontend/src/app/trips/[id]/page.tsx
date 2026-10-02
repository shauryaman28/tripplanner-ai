"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  CalendarDays,
  CalendarMinus,
  ChevronRight,
  CircleAlert,
  MessageCircleQuestion,
  Plane,
  Sparkles,
  TriangleAlert,
  Users,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import AgentProgressPanel from "@/components/AgentProgressPanel";
import AppHeader from "@/components/AppHeader";
import ChatInput from "@/components/ChatInput";
import ItineraryView from "@/components/ItineraryView";
import MessageThread, { AssistantAvatar, type Message } from "@/components/MessageThread";
import { TripStatusBadge } from "@/components/ui";
import { clarifyTrip, getItinerary, getToken, getTrip, getTripStatus, planTrip, refineTrip, replanTrip } from "@/lib/api";
import { formatDateRange, formatINR, nightsBetween, plural } from "@/lib/format";
import { WHOLE_RUN, deriveAgentStates, useSSE, type RunScope } from "@/lib/sse";
import type { AgentStatus, BudgetConflict, BudgetConflictOption, Itinerary, ReplanChoice, Trip, TripStatus } from "@/lib/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function uid() {
  return Math.random().toString(36).slice(2);
}

function errorText(err: unknown) {
  return err instanceof Error ? err.message : "unknown error";
}

/** What the assistant says about a budget conflict. A refinement that hit one leaves the plan as it was. */
function conflictMessage({ reason, options }: BudgetConflict, keptItinerary = false) {
  if (keptItinerary) return `${reason} Your itinerary is unchanged.`;
  return `${reason} ${options.length > 0 ? "Pick one of the options, or describe a different trip." : "Describe a different trip to try again."}`;
}

/** The two-column layout, where the assistant stays beside the plan instead of below it. */
function isWideScreen() {
  return window.matchMedia("(min-width: 1024px)").matches;
}

type PlanningPhase =
  | "loading"       // fetching the trip
  | "idle"          // trip exists, nothing started
  | "planning"      // agents running
  | "clarifying"    // backend returned clarification_needed
  | "complete"      // itinerary ready
  | "failed"        // last run failed and there is no itinerary to show
  | "refining";     // refinement in progress (previous itinerary stays visible)

/** How a run that produced no itinerary ended. */
interface RunFailure {
  error?: string;
  conflict?: BudgetConflict | null;
}

const POLL_MS = 3_000;

// One tap instead of typing — each is sent to the assistant as written.
const STARTERS = [
  "A relaxed trip with beaches and good food",
  "Pack in history, culture and sightseeing",
  "Keep it budget-friendly and close to nature",
];
const REFINEMENTS = ["Make it cheaper", "Switch to a nicer hotel", "Swap in more history", "Add a day"];

const PHASE_BADGE: Record<Exclude<PlanningPhase, "loading">, { status: TripStatus; label?: string }> = {
  idle:       { status: "pending" },
  planning:   { status: "planning" },
  refining:   { status: "planning", label: "Updating…" },
  clarifying: { status: "pending", label: "Needs your answer" },
  complete:   { status: "completed" },
  failed:     { status: "failed" },
};

// A targeted refinement repeats one search; any other kind plans the whole trip again.
const TARGETED: Record<string, { agent: string; reply: string }> = {
  targeted_flights:    { agent: "flight_agent",     reply: "On it — looking for different flights…" },
  targeted_hotel:      { agent: "hotel_agent",      reply: "On it — looking for a different place to stay…" },
  targeted_activities: { agent: "activities_agent", reply: "On it — looking for different things to do…" },
};

const REPLAN_ICONS: Record<ReplanChoice, LucideIcon> = {
  cheaper_flights: Plane,
  reduce_days:     CalendarMinus,
  increase_budget: Wallet,
};

// ---------------------------------------------------------------------------
// Pieces of the page
// ---------------------------------------------------------------------------

function TripHero({ trip, phase }: { trip: Trip | null; phase: PlanningPhase }) {
  if (!trip || phase === "loading") {
    return (
      <div aria-hidden>
        <div className="skeleton h-12 w-64" />
        <div className="skeleton mt-4 h-5 w-96 max-w-full" />
      </div>
    );
  }

  const nights = nightsBetween(trip.start_date, trip.end_date);
  return (
    <header className="animate-fade">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <h1 className="text-balance font-display text-4xl font-medium leading-[1.05] tracking-tight text-ink-900 sm:text-5xl">
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
            <li key={interest} className="chip">
              {interest}
            </li>
          ))}
        </ul>
      )}
    </header>
  );
}

/** One-tap messages. `compact` is the size that sits above the composer. */
function SuggestionChips({
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
function StartStage({ destination, onPick }: { destination: string; onPick: (text: string) => void }) {
  return (
    <div className="card relative animate-rise overflow-hidden p-7 sm:p-10">
      <div className="pointer-events-none absolute -right-16 -top-20 h-64 w-64 rounded-full bg-sunset opacity-20 blur-3xl" aria-hidden />
      <AssistantAvatar size="h-11 w-11" />
      <h2 className="mt-5 text-balance font-display text-3xl font-medium tracking-tight text-ink-900">Let&apos;s plan {destination}</h2>
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

function ClarifyStage({ question }: { question: string | null }) {
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

/** A run is in flight and there is no itinerary to show yet. */
function PlanningStage({ destination, agents }: { destination: string; agents: Record<string, string> }) {
  const step =
    agents.flight_agent === "running"
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
            <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">Planning {destination}…</h2>
            <p className="mt-0.5 text-sm text-ink-600" aria-live="polite">
              {step}
            </p>
          </div>
        </div>
        <div className="mt-6 h-1.5 overflow-hidden rounded bg-ink-100" aria-hidden>
          <div className="h-full w-2/5 animate-indeterminate rounded bg-ink-900" />
        </div>
      </div>
      {/* stand-ins for the plan — on a small screen they would only push the live progress further down */}
      <div className="skeleton hidden h-44 lg:block" aria-hidden />
      <div className="skeleton hidden h-44 lg:block" aria-hidden />
    </div>
  );
}

/** The last run failed and nothing can be shown: explain, and offer the way forward. */
function FailedStage({
  conflict,
  onReplan,
  onPick,
  disabled,
}: {
  conflict: BudgetConflict | null;
  onReplan: (option: BudgetConflictOption) => void;
  onPick: (text: string) => void;
  disabled: boolean;
}) {
  if (conflict) {
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

        <div className="mt-7 grid gap-3 empty:hidden sm:grid-cols-3">
          {conflict.options.map((option) => {
            const Icon = REPLAN_ICONS[option.choice] ?? Wallet;
            return (
              <button
                key={option.choice}
                onClick={() => onReplan(option)}
                disabled={disabled}
                className="focus-ring group rounded-2xl border border-ink-200 bg-white p-4 text-left transition
                           hover:-translate-y-0.5 hover:border-ink-900 hover:shadow-lift disabled:pointer-events-none disabled:opacity-50"
              >
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-ink-100 text-ink-700 transition-colors group-hover:bg-ink-900 group-hover:text-white">
                  <Icon className="h-[18px] w-[18px]" aria-hidden />
                </span>
                <p className="mt-3 text-sm font-medium leading-snug text-ink-900">{option.description}</p>
                <p className="mt-1 text-xs text-ink-500">{option.estimated_saving}</p>
              </button>
            );
          })}
        </div>
        <p className="mt-5 text-sm text-ink-500">
          {conflict.options.length > 0 ? "Or describe" : "Describe"} a different trip to the assistant.
        </p>
      </div>
    );
  }

  return (
    <div className="card animate-rise p-7 sm:p-10">
      <span className="grid h-11 w-11 place-items-center rounded-full bg-bad-soft text-bad-ink">
        <CircleAlert className="h-5 w-5" aria-hidden />
      </span>
      <h2 className="mt-5 font-display text-3xl font-medium tracking-tight text-ink-900">That plan didn&apos;t come together</h2>
      <p className="mt-2 max-w-prose text-ink-600">
        Nothing was saved. The assistant panel has the details — tell it what to change, or try one of these.
      </p>
      <div className="mt-6">
        <SuggestionChips ideas={STARTERS} onPick={onPick} disabled={disabled} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function TripDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const tripId = params.id;

  const [trip, setTrip]                 = useState<Trip | null>(null);
  const [phase, setPhase]               = useState<PlanningPhase>("loading");
  const [messages, setMessages]         = useState<Message[]>([]);
  const [itinerary, setItinerary]       = useState<Itinerary | null>(null);
  const [sending, setSending]           = useState(false);   // a POST is in flight
  const [clarifyQ, setClarifyQ]         = useState<string | null>(null);
  const [conflict, setConflict]         = useState<BudgetConflict | null>(null);
  const [polledAgents, setPolledAgents] = useState<Record<string, AgentStatus>>({});
  const [scope, setScope]               = useState<RunScope>(WHOLE_RUN);
  const [delivered, setDelivered]       = useState(0);       // plans a run has handed over on this page
  const [composerInView, setComposerInView] = useState(true);

  const planRef     = useRef<HTMLDivElement>(null);
  const threadRef   = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);

  const running = phase === "planning" || phase === "refining";

  // The stream stays open for the life of the page, so it is already subscribed
  // when a run starts — pub/sub has no replay for late subscribers.
  const { events, status: sseStatus, reset: resetEvents } = useSSE(tripId, phase !== "loading");

  // Refs so the stable callbacks below always see current values.
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const itineraryRef = useRef(itinerary);
  itineraryRef.current = itinerary;
  const eventsRef = useRef(events);
  const agentStatesRef = useRef<RunScope["carried"]>({});
  // True while a run's outcome is still owed; SSE and polling race to report it.
  const awaitingOutcome = useRef(false);

  const addMessage = useCallback((role: Message["role"], text: string) => {
    setMessages((prev) => [...prev, { role, text, id: uid() }]);
  }, []);

  useEffect(() => {
    if (trip) document.title = `${trip.destination} · TripPlanner AI`;
  }, [trip]);

  // Keep the newest message in view by scrolling the thread itself — never the page.
  useEffect(() => {
    const thread = threadRef.current;
    if (thread) thread.scrollTop = thread.scrollHeight;
  }, [messages.length]);

  // Small screens stack the plan above the assistant: a run that delivers a plan scrolls up to
  // it, and a floating button leads back down to the composer whenever that is off screen.
  useEffect(() => {
    if (delivered > 0 && !isWideScreen()) planRef.current?.scrollIntoView({ block: "start" });
  }, [delivered]);

  useEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    const observer = new IntersectionObserver(([entry]) => setComposerInView(entry.isIntersecting));
    observer.observe(composer);
    return () => observer.disconnect();
  }, []);

  // ── Load the trip and whatever state it is already in ──────────────────
  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const loaded = await getTrip(tripId);
        const [existing, progress] = await Promise.all([
          loaded.status === "pending" ? null : getItinerary(tripId).catch(() => null), // never planned → nothing to fetch
          getTripStatus(tripId).catch(() => null),
        ]);
        if (cancelled) return;
        setTrip(loaded);
        setItinerary(existing);
        if (progress) setPolledAgents(progress.progress.agents); // the stream has no history to replay
        if (loaded.status === "planning") {
          awaitingOutcome.current = true;
          setPhase(existing ? "refining" : "planning");
        } else if (existing) {
          setPhase("complete");
        } else if (loaded.status === "failed") {
          const found = progress?.budget_conflict ?? null;
          setConflict(found);
          setPhase("failed");
          addMessage("assistant", found ? conflictMessage(found) : "The last planning attempt failed. Describe your trip to try again.");
        } else {
          setPhase("idle");
        }
      } catch {
        if (!cancelled) router.replace(getToken() ? "/trips" : "/login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tripId, router, addMessage]);

  // While a run is in flight GET /status only counts that run's searches; once it is over,
  // it counts every search behind the plan — which is what the panel should settle on.
  const refreshProgress = useCallback(() => {
    getTripStatus(tripId)
      .then((current) => setPolledAgents(current.progress.agents))
      .catch(() => {});
  }, [tripId]);

  // ── A run ended. SSE and polling can both report it — handle it once. ──
  const finishRun = useCallback(
    async (succeeded: boolean, failure: RunFailure = {}) => {
      if (!awaitingOutcome.current) return;
      awaitingOutcome.current = false; // claim it before any await

      const previous = itineraryRef.current; // a refinement keeps this on screen whatever happens
      let { error } = failure;
      if (succeeded) {
        try {
          const [latest, refreshed] = await Promise.all([getItinerary(tripId), getTrip(tripId)]);
          const total = formatINR(latest.total_cost ?? latest.structured_data?.total_cost ?? 0);
          if (latest.id === previous?.id) {
            // Polling sees a refinement that failed as "completed" — the plan it left alone still
            // stands — so an unchanged itinerary is the only sign that nothing happened.
            addMessage("assistant", "That change couldn't be made, so your itinerary is unchanged.");
          } else if (previous && JSON.stringify(latest.structured_data) === JSON.stringify(previous.structured_data)) {
            addMessage("assistant", "I searched again and the plan came out the same. Tell me more about what you'd like instead.");
          } else {
            addMessage(
              "assistant",
              previous ? `Done — the plan now comes to ${total}.` : `Your itinerary is ready — ${total} in total. Ask me for any change.`,
            );
            setDelivered((count) => count + 1);
          }
          refreshProgress();
          setItinerary(latest);
          setTrip(refreshed); // a refinement may have moved the dates or the destination
          setPhase("complete");
          return;
        } catch {
          error = "the itinerary could not be loaded";
        }
      }

      const conflict = succeeded ? null : failure.conflict ?? null;
      if (conflict) {
        if (!previous) setConflict(conflict);
        addMessage("assistant", conflictMessage(conflict, previous !== null));
      } else if (error) {
        addMessage("assistant", `Planning failed: ${error.replace(/\.+$/, "")}.`);
      }
      // a failed run still corrects what it learned (e.g. the destination) — show it
      getTrip(tripId).then(setTrip).catch(() => {});
      refreshProgress();
      setPhase(previous ? "complete" : "failed");
    },
    [tripId, addMessage, refreshProgress],
  );

  // ── React to every new SSE event (several can arrive in one render) ────
  const handled = useRef(0);
  // A conflict is announced just before the run is reported failed; held here until then.
  const announcedConflict = useRef<BudgetConflict | null>(null);
  useEffect(() => {
    for (const ev of events.slice(Math.min(handled.current, events.length))) {
      if (ev.event === "planning_started") {
        announcedConflict.current = null;
      } else if (ev.event === "budget_conflict") {
        announcedConflict.current = { reason: ev.reason ?? "Flights exceed the budget.", options: ev.options ?? [] };
      } else if (ev.event === "planning_complete") {
        finishRun(true);
      } else if (ev.event === "planning_failed") {
        finishRun(false, { error: ev.error ?? "unknown error", conflict: announcedConflict.current });
      }
    }
    handled.current = events.length;
  }, [events, finishRun]);

  // ── Polling fallback: catches a run that ended while the stream was down ─
  useEffect(() => {
    if (!running || sending) return;
    const timer = setInterval(async () => {
      try {
        const current = await getTripStatus(tripId);
        setPolledAgents(current.progress.agents);
        if (current.status === "completed") finishRun(true);
        else if (current.status === "failed") {
          finishRun(false, { error: "see the planning progress for details", conflict: current.budget_conflict });
        }
      } catch {
        // transient — the next tick retries
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [running, sending, tripId, finishRun]);

  // ── Start a run: POST, then let SSE / polling report how it ends ───────
  const launch = useCallback(
    async (next: "planning" | "refining", call: () => Promise<string>) => {
      const previous = phaseRef.current;
      if (next === "planning") {
        // a fresh plan starts every search again
        resetEvents();
        setPolledAgents({});
        setScope(WHOLE_RUN);
      } else {
        // A refinement carries forward the searches it does not repeat. Until the backend says
        // which one that is (see handleChatSubmit), all three keep the state they have.
        setScope({ since: eventsRef.current.length, carried: agentStatesRef.current });
      }
      setConflict(null);
      setSending(true);
      setPhase(next);
      awaitingOutcome.current = true;
      try {
        addMessage("assistant", await call());
      } catch (err: unknown) {
        awaitingOutcome.current = false;
        setPhase(previous);
        addMessage("assistant", `Something went wrong: ${errorText(err)}`);
      } finally {
        setSending(false);
      }
    },
    [resetEvents, addMessage],
  );

  const startPlanning = useCallback(
    (rawInput: string) => {
      addMessage("user", rawInput);
      launch("planning", async () => {
        const res = await planTrip(tripId, { raw_input: rawInput });
        if (res.status === "clarification_needed") {
          awaitingOutcome.current = false; // nothing is running — the backend asked a question instead
          setClarifyQ(res.question);
          setPhase("clarifying");
          return res.question;
        }
        return "On it! I'm searching for flights, a place to stay and things to do…";
      });
    },
    [tripId, addMessage, launch],
  );

  function handleChatSubmit(text: string) {
    if (phase === "clarifying") {
      addMessage("user", text);
      setClarifyQ(null);
      launch("planning", async () => {
        await clarifyTrip(tripId, text);
        return "Got it, searching now…";
      });
    } else if (phase === "complete") {
      addMessage("user", text);
      launch("refining", async () => {
        const res = await refineTrip(tripId, text);
        const targeted = TARGETED[res.refinement_type];
        setScope(({ since, carried }) => ({
          since,
          carried: targeted ? Object.fromEntries(Object.entries(carried).filter(([agent]) => agent !== targeted.agent)) : {},
        }));
        return targeted?.reply ?? "On it — planning the trip again with that change…";
      });
    } else if (phase === "idle" || phase === "failed") {
      startPlanning(text);
    }
  }

  function chooseReplan(option: BudgetConflictOption) {
    addMessage("user", option.description);
    launch("planning", async () => {
      await replanTrip(tripId, option.choice);
      return "Re-planning with that change…";
    });
  }

  const busy = running || sending || phase === "loading";
  const chatPlaceholder =
    phase === "clarifying"
      ? "Type your answer…"
      : phase === "complete"
      ? "Ask for a change…"
      : phase === "idle" || phase === "failed"
      ? "Tell me about your trip…"
      : phase === "refining"
      ? "Updating your plan…"
      : "Planning in progress…";

  const agentStates = deriveAgentStates(events, polledAgents, running, scope);
  agentStatesRef.current = agentStates;
  eventsRef.current = events;
  const destination = trip?.destination ?? "your trip";
  // The searches are worth showing while they run and whenever one has something to report; a
  // plan loaded from an earlier visit, with all three done, has nothing to add.
  const showProgress = running || events.length > 0 || phase === "failed" || Object.values(polledAgents).includes("failed");
  // Before there is a plan the left column is only an invitation; on a small screen the assistant says it all.
  const invitationOnly = phase === "idle" || phase === "clarifying";

  function focusComposer() {
    composerRef.current?.scrollIntoView({ block: "center" });
    composerRef.current?.querySelector("textarea")?.focus({ preventScroll: true });
  }

  return (
    <div className="min-h-dvh">
      <AppHeader>
        <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-sm">
          <Link href="/trips" className="focus-ring rounded text-ink-500 transition-colors hover:text-ink-900">
            Trips
          </Link>
          <ChevronRight className="h-4 w-4 shrink-0 text-ink-300" aria-hidden />
          <span className="truncate font-medium text-ink-900">{trip?.destination ?? "…"}</span>
        </nav>
      </AppHeader>

      <main className="mx-auto max-w-7xl px-4 pb-12 pt-8 sm:px-6 lg:px-8 lg:pt-10">
        <TripHero trip={trip} phase={phase} />

        <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
          {/* ── The plan ───────────────────────────────────────────────── */}
          <div ref={planRef} className={`min-w-0 ${invitationOnly ? "hidden lg:block" : ""}`}>
            {phase === "loading" && (
              <div className="space-y-4" aria-hidden>
                <div className="skeleton h-48" />
                <div className="skeleton h-44" />
              </div>
            )}
            {phase === "idle" && <StartStage destination={destination} onPick={startPlanning} />}
            {phase === "clarifying" && <ClarifyStage question={clarifyQ} />}
            {phase === "planning" && <PlanningStage destination={destination} agents={agentStates} />}
            {phase === "failed" && (
              <FailedStage conflict={conflict} onReplan={chooseReplan} onPick={startPlanning} disabled={busy} />
            )}
            {itinerary && (phase === "complete" || phase === "refining") && (
              <ItineraryView itinerary={itinerary} trip={trip} updating={phase === "refining"} />
            )}
          </div>

          {/* ── The assistant ──────────────────────────────────────────── */}
          {/* Beside the plan on a wide screen: as tall as the conversation, up to the height of the
              screen — then the thread scrolls. Below the plan on a small one. */}
          <aside className="flex min-w-0 flex-col gap-3 lg:sticky lg:top-24 lg:max-h-[calc(100dvh-7.5rem)]">
            {/* data-stream: whether live updates are flowing, for the browser tests (the badge only shows during a run) */}
            <div className="card flex min-h-0 flex-col overflow-hidden" data-stream={sseStatus}>
              <div className="flex items-center gap-3 border-b border-ink-200/70 px-4 py-3.5">
                <AssistantAvatar size="h-9 w-9" />
                <div>
                  <p className="text-sm font-semibold text-ink-900">Trip assistant</p>
                  <p className="text-xs text-ink-500">Plans the trip, then changes it when you ask</p>
                </div>
              </div>

              {showProgress && (
                <div className="animate-fade border-b border-ink-200/70 px-4 py-3.5">
                  <AgentProgressPanel events={events} sseStatus={sseStatus} active={running} polled={polledAgents} scope={scope} />
                </div>
              )}

              <div ref={threadRef} className="max-h-[45dvh] min-h-0 overflow-y-auto px-4 py-4 lg:max-h-none lg:flex-1">
                {messages.length > 0 ? (
                  <MessageThread messages={messages} />
                ) : (
                  <p className="text-sm leading-relaxed text-ink-500">
                    {phase === "complete"
                      ? "Your itinerary is ready. Ask for any change — a different hotel, cheaper flights, more of what you like — and only that part is re-planned."
                      : "Describe the trip you have in mind and I'll take it from there."}
                  </p>
                )}
              </div>
            </div>

            <div ref={composerRef}>
              {phase === "complete" && (
                <div className="mb-2.5">
                  <SuggestionChips compact ideas={REFINEMENTS} onPick={handleChatSubmit} label="Suggested changes" />
                </div>
              )}
              {/* small screens only — a wide one offers these in the invitation beside the assistant */}
              {phase === "idle" && (
                <div className="mb-2.5 lg:hidden">
                  <SuggestionChips compact ideas={STARTERS} onPick={startPlanning} label="Suggested trips" />
                </div>
              )}
              <ChatInput onSubmit={handleChatSubmit} disabled={running || phase === "loading"} loading={sending} placeholder={chatPlaceholder} />
              <p className="mt-1.5 hidden text-center text-[11px] text-ink-500 lg:block">Enter to send · Shift + Enter for a new line</p>
            </div>
          </aside>
        </div>
      </main>

      {/* Small screens: the composer sits below the whole plan, so keep it one tap away */}
      {phase === "complete" && !composerInView && (
        // centred: the right edge of the day cards is where their "Map" buttons are
        <div className="pointer-events-none fixed inset-x-0 bottom-[max(1rem,env(safe-area-inset-bottom))] z-30 flex justify-center lg:hidden">
          <button
            onClick={focusComposer}
            className="focus-ring pointer-events-auto inline-flex animate-rise items-center gap-2 rounded-full bg-ink-900 py-3 pl-3.5 pr-4
                       text-sm font-medium text-white shadow-float transition active:scale-95"
          >
            <Sparkles className="h-4 w-4 text-saffron" aria-hidden />
            Ask for a change
          </button>
        </div>
      )}
    </div>
  );
}
