"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ChevronRight, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import AgentProgressPanel from "@/components/AgentProgressPanel";
import AppHeader from "@/components/AppHeader";
import ChatInput from "@/components/ChatInput";
import ItineraryView, { type Updating } from "@/components/ItineraryView";
import MessageThread, { AssistantAvatar, type Message } from "@/components/MessageThread";
import ProgressSheet from "@/components/ProgressSheet";
import {
  ClarifyStage,
  FailedStage,
  PlanningStage,
  STARTERS,
  StartStage,
  SuggestionChips,
  TripHero,
  type PlanningPhase,
} from "@/components/TripStages";
import { clarifyTrip, getItinerary, getToken, getTrip, getTripStatus, planTrip, refineTrip, replanTrip, retryTrip } from "@/lib/api";
import { SECTION_OF_AGENT, SECTION_OF_REFINEMENT, changedParts, describeChanges } from "@/lib/changes";
import { draftProgress, readDraft } from "@/lib/draft";
import { formatINR, plural } from "@/lib/format";
import { SEARCH_WORDS, WHOLE_RUN, agentFailures, deriveAgentStates, useSSE, type RunScope } from "@/lib/sse";
import type { AgentStatus, BudgetConflict, BudgetConflictOption, Itinerary, Trip, TripStatusResponse } from "@/lib/types";
import { isWideScreen, useWideScreen } from "@/lib/useWideScreen";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function uid() {
  return Math.random().toString(36).slice(2);
}

function errorText(err: unknown) {
  return err instanceof Error ? err.message : "unknown error";
}

const withoutFullStop = (text: string) => text.replace(/\.+$/, "");

/** What the assistant says about a budget conflict. A refinement that hit one leaves the plan as it was. */
function conflictMessage({ reason, options }: BudgetConflict, keptItinerary = false) {
  if (keptItinerary) return `${reason} Your itinerary is unchanged.`;
  return `${reason} ${options.length > 0 ? "Pick one of the options, or describe a different trip." : "Describe a different trip to try again."}`;
}

/** How a run that produced no itinerary ended. */
interface RunFailure {
  error?: string;
  conflict?: BudgetConflict | null;
}

const POLL_MS = 3_000;
// How long the parts a change touched stay marked "Updated" (the green tint itself fades sooner).
const CHANGED_MS = 5_000;
const NOTHING_CHANGED: ReadonlySet<string> = new Set();

const REFINEMENTS = ["Make it cheaper", "Switch to a nicer hotel", "Swap in more history", "Add a day"];

// A targeted refinement repeats one search; any other kind plans the whole trip again.
const TARGETED: Record<string, { agent: string; reply: string }> = {
  targeted_flights:    { agent: "flight_agent",     reply: "On it — looking for different flights…" },
  targeted_hotel:      { agent: "hotel_agent",      reply: "On it — looking for a different place to stay…" },
  targeted_activities: { agent: "activities_agent", reply: "On it — looking for different things to do…" },
};

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
  const [failureReason, setFailureReason] = useState<string | null>(null);
  const [polledAgents, setPolledAgents] = useState<Record<string, AgentStatus>>({});
  const [polledErrors, setPolledErrors] = useState<Record<string, string>>({});
  const [polledRetryable, setPolledRetryable] = useState<Record<string, boolean>>({});
  const [scope, setScope]               = useState<RunScope>(WHOLE_RUN);
  const [updating, setUpdating]         = useState<Updating>(null);   // what the run in flight is doing to the plan on screen
  const [changed, setChanged]           = useState<ReadonlySet<string>>(NOTHING_CHANGED);  // what the last change touched
  const [delivered, setDelivered]       = useState(0);       // plans a run has handed over on this page
  const [composerInView, setComposerInView] = useState(true);
  const [sheetOpen, setSheetOpen]       = useState(false);   // the progress sheet of a small screen, opened

  const planRef     = useRef<HTMLDivElement>(null);
  const threadRef   = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);

  const running = phase === "planning" || phase === "refining";
  const wide = useWideScreen();

  // The stream stays open for the life of the page, so it is already subscribed
  // when a run starts — pub/sub has no replay for late subscribers.
  const { events, status: sseStatus, draft, reset: resetEvents } = useSSE(tripId, phase !== "loading");

  // Refs so the stable callbacks below always see current values.
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const itineraryRef = useRef(itinerary);
  itineraryRef.current = itinerary;
  const tripRef = useRef(trip);
  tripRef.current = trip;
  const eventsRef = useRef(events);
  const agentStatesRef = useRef<RunScope["carried"]>({});
  // True while a run's outcome is still owed; SSE and polling race to report it.
  const awaitingOutcome = useRef(false);
  // The search a retry in flight repeats (null for any other run) — it changes how the outcome is worded.
  const retrying = useRef<string | null>(null);

  const addMessage = useCallback((role: Message["role"], text: string, points?: string[]) => {
    setMessages((prev) => [...prev, { role, text, points, id: uid() }]);
  }, []);

  /** What GET /status knows about the searches: how each ended and, for one that failed, why — and whether to offer a retry. */
  const applyProgress = useCallback((current: TripStatusResponse) => {
    setPolledAgents(current.progress.agents);
    setPolledErrors(current.progress.errors ?? {});
    setPolledRetryable(current.progress.retryable ?? {});
  }, []);

  useEffect(() => {
    if (trip) document.title = `${trip.destination} · TripPlanner AI`;
  }, [trip]);

  // Keep the newest message in view by scrolling the thread itself — never the page. A reply
  // taller than the thread (a list of changes) is shown from its first line, not its last.
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
    const newest = thread.querySelector<HTMLElement>("[aria-live] > :last-child");
    const cutOff = newest ? thread.getBoundingClientRect().top - newest.getBoundingClientRect().top : 0;
    if (cutOff > 0) thread.scrollTop -= cutOff + 12;
  }, [messages.length]);

  // Small screens stack the plan above the assistant: a run that delivers a plan scrolls up to
  // it, and a floating button leads back down to the composer whenever that is off screen.
  useEffect(() => {
    if (delivered > 0 && !isWideScreen()) planRef.current?.scrollIntoView({ block: "start" });
  }, [delivered]);

  // The same on a small screen when a first plan starts: the itinerary is written in the plan
  // column, which is above the composer the request was just sent from.
  useEffect(() => {
    if (phase === "planning" && !isWideScreen()) planRef.current?.scrollIntoView({ block: "start" });
  }, [phase]);

  useEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    const observer = new IntersectionObserver(([entry]) => setComposerInView(entry.isIntersecting));
    observer.observe(composer);
    return () => observer.disconnect();
  }, []);

  // What a change touched is marked for a few seconds, then the plan is just the plan again.
  useEffect(() => {
    if (changed.size === 0) return;
    const timer = setTimeout(() => setChanged(NOTHING_CHANGED), CHANGED_MS);
    return () => clearTimeout(timer);
  }, [changed]);

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
        if (progress) applyProgress(progress); // the stream has no history to replay
        if (loaded.status === "planning") {
          awaitingOutcome.current = true;
          // GET /status says what the run in flight is — the event that announced it has gone by.
          retrying.current = progress?.run?.retry ?? null;
          if (existing) setUpdating(SECTION_OF_REFINEMENT[progress?.run?.refinement_type ?? ""] ?? "all");
          setPhase(existing ? "refining" : "planning");
        } else if (existing) {
          setPhase("complete");
        } else if (loaded.status === "failed") {
          const found = progress?.budget_conflict ?? null;
          const why = progress?.failure_reason ?? null;
          setConflict(found);
          setFailureReason(why);
          setPhase("failed");
          addMessage(
            "assistant",
            found ? conflictMessage(found)
            : why ? `The last attempt failed: ${withoutFullStop(why)}.`
            : "The last planning attempt failed. Describe your trip to try again.",
          );
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
  }, [tripId, router, addMessage, applyProgress]);

  // While a run is in flight GET /status only counts that run's searches; once it is over,
  // it counts every search behind the plan — which is what the panel should settle on.
  const refreshProgress = useCallback(() => {
    getTripStatus(tripId).then(applyProgress).catch(() => {});
  }, [tripId, applyProgress]);

  // ── A run ended. SSE and polling can both report it — handle it once. ──
  const finishRun = useCallback(
    async (succeeded: boolean, failure: RunFailure = {}) => {
      if (!awaitingOutcome.current) return;
      awaitingOutcome.current = false; // claim it before any await

      const previous = itineraryRef.current; // a change keeps this on screen whatever happens
      const retried = retrying.current;      // the search this run repeated, if it was a retry
      retrying.current = null;
      let { error } = failure;
      if (succeeded) {
        try {
          const [latest, refreshed] = await Promise.all([getItinerary(tripId), getTrip(tripId)]);
          const total = formatINR(latest.total_cost ?? latest.structured_data?.total_cost ?? 0);
          // After a change request: say what is different now, by comparing the two plans.
          const summary =
            previous?.structured_data && latest.structured_data
              ? describeChanges(previous.structured_data, latest.structured_data, { before: tripRef.current, after: refreshed })
              : null;
          if (latest.id === previous?.id) {
            // Polling sees a change that failed as "completed" — the plan it left alone still
            // stands — so an unchanged itinerary is the only sign that nothing happened.
            addMessage(
              "assistant",
              retried
                ? `The ${SEARCH_WORDS[retried]} search failed again, so your itinerary is unchanged.`
                : "That change couldn't be made, so your itinerary is unchanged.",
            );
          } else if (summary && !summary.changed) {
            addMessage("assistant", "I searched again and the plan came out the same. Tell me more about what you'd like instead.");
          } else if (summary) {
            addMessage("assistant", `Done. ${summary.headline}`, summary.points);
            // …and show it on the plan itself: the parts that came back different are marked.
            setChanged(changedParts(previous!.structured_data!, latest.structured_data!));
            setDelivered((count) => count + 1);
          } else {
            addMessage("assistant", `Your itinerary is ready — ${total} in total. Ask me for any change.`);
            setDelivered((count) => count + 1);
          }
          refreshProgress();
          setItinerary(latest);
          setTrip(refreshed); // a refinement may have moved the dates or the destination
          setUpdating(null);
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
      } else if (error && retried) {
        addMessage("assistant", `The ${SEARCH_WORDS[retried]} search failed again: ${withoutFullStop(error)}. Your itinerary is unchanged.`);
      } else if (error) {
        addMessage("assistant", `Planning failed: ${withoutFullStop(error)}.`);
      }
      if (!previous) setFailureReason(conflict ? null : error ?? null);
      // a failed run still corrects what it learned (e.g. the destination) — show it
      getTrip(tripId).then(setTrip).catch(() => {});
      refreshProgress();
      setUpdating(null);
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
        applyProgress(current);
        if (current.status === "completed") finishRun(true);
        else if (current.status === "failed") {
          finishRun(false, {
            error: current.failure_reason ?? "see the planning progress for details",
            conflict: current.budget_conflict,
          });
        }
      } catch {
        // transient — the next tick retries
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [running, sending, tripId, finishRun, applyProgress]);

  // ── Start a run: POST, then let SSE / polling report how it ends ───────
  const launch = useCallback(
    async (next: "planning" | "refining", call: () => Promise<string>, touching: Updating = next === "refining" ? "pending" : null) => {
      const previous = phaseRef.current;
      if (next === "planning") {
        // a fresh plan starts every search again
        resetEvents();
        setPolledAgents({});
        setPolledErrors({});
        setPolledRetryable({});
        setScope(WHOLE_RUN);
      } else {
        // A change carries forward the searches it does not repeat. Until the backend says
        // which one that is (see handleChatSubmit), all three keep the state they have.
        setScope({ since: eventsRef.current.length, carried: agentStatesRef.current });
      }
      setConflict(null);
      setFailureReason(null);
      setChanged(NOTHING_CHANGED);
      setUpdating(touching);
      setSending(true);
      setPhase(next);
      awaitingOutcome.current = true;
      try {
        addMessage("assistant", await call());
      } catch (err: unknown) {
        // The request can fail on the way back (a proxy timeout) after the backend has started
        // the run. Ask before giving up, or the page stops listening to a run that is in flight.
        const current = await getTripStatus(tripId).catch(() => null);
        if (current?.status === "planning") {
          addMessage("assistant", "That took longer than usual to start, but it's running now…");
        } else {
          awaitingOutcome.current = false;
          retrying.current = null;
          setUpdating(null);
          setPhase(previous);
          addMessage("assistant", `Something went wrong: ${errorText(err)}`);
        }
      } finally {
        setSending(false);
      }
    },
    [tripId, resetEvents, addMessage],
  );

  /** Once it is known which search a change repeats: the other two keep the result they have. */
  const repeatOnly = useCallback((agent: string | undefined) => {
    setScope(({ since, carried }) => ({
      since,
      carried: agent ? Object.fromEntries(Object.entries(carried).filter(([name]) => name !== agent)) : {},
    }));
  }, []);

  /** POST /plan and POST /retry can both answer with a question instead of starting a run. */
  const askInstead = useCallback((question: string) => {
    awaitingOutcome.current = false; // nothing is running — the backend asked a question instead
    setClarifyQ(question);
    setPhase("clarifying");
    return question;
  }, []);

  const startPlanning = useCallback(
    (rawInput: string) => {
      addMessage("user", rawInput);
      launch("planning", async () => {
        const res = await planTrip(tripId, { raw_input: rawInput });
        if (res.status === "clarification_needed") return askInstead(res.question);
        return "On it! I'm searching for flights, a place to stay and things to do…";
      });
    },
    [tripId, addMessage, launch, askInstead],
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
        repeatOnly(targeted?.agent);
        // only now is it known what the change touches: one section, or the whole plan
        setUpdating(SECTION_OF_REFINEMENT[res.refinement_type] ?? "all");
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

  /** A search failed and the plan was built without it: run just that search again. */
  function retrySearch(agent: string) {
    addMessage("user", `Retry the ${SEARCH_WORDS[agent]} search`);
    retrying.current = agent;
    launch(
      "refining",
      async () => {
        await retryTrip(tripId, agent);
        repeatOnly(agent);
        return `Trying the ${SEARCH_WORDS[agent]} search again…`;
      },
      SECTION_OF_AGENT[agent] ?? "all",
    );
  }

  /** The run failed and there is no plan: plan the trip again as it is. */
  function retryPlan() {
    addMessage("user", "Try again");
    launch("planning", async () => {
      const res = await retryTrip(tripId);
      if (res.status === "clarification_needed") return askInstead(res.question);
      return "Trying again — searching for flights, a place to stay and things to do…";
    });
  }

  const busy = running || sending || phase === "loading";
  const refinable = phase === "complete" || phase === "refining";
  const chatPlaceholder =
    phase === "clarifying"
      ? "Type your answer…"
      : phase === "complete"
      ? "a nicer hotel, cheaper flights…"
      : phase === "idle" || phase === "failed"
      ? "Tell me about your trip…"
      : phase === "refining"
      ? "Updating your plan…"
      : "Planning in progress…";

  const agentStates = deriveAgentStates(events, polledAgents, running, scope);
  agentStatesRef.current = agentStates;
  eventsRef.current = events;
  const failures = agentFailures(events, agentStates, polledErrors, scope);
  const failedSearches = Object.keys(failures);
  const searchesDone = Object.values(agentStates).filter((state) => state === "completed" || state === "failed").length;
  // Where the builder has got to, for the progress panel: undefined until it starts writing.
  const writing = useMemo(() => (running && draft ? draftProgress(readDraft(draft.text)) : undefined), [running, draft]);

  const destination = trip?.destination ?? "your trip";
  // The searches are worth showing while they run and whenever one has something to report; a
  // plan loaded from an earlier visit, with all three done, has nothing to add.
  const showProgress = running || events.length > 0 || phase === "failed" || failedSearches.length > 0;
  // On a small screen the panel lives in a sheet at the foot of the screen — while a run is in
  // flight, and for as long as a search that failed is waiting to be retried.
  const showSheet = !wide && (running || (phase === "complete" && failedSearches.length > 0));
  // The composer is below the whole plan there, so a button leads back to it when it is off screen
  // — but never over an open sheet, where it would cover what the sheet shows.
  const showComposerButton = !wide && phase === "complete" && !composerInView && !(showSheet && sheetOpen);
  // Before there is a plan the left column is only an invitation; on a small screen the assistant says it all.
  const invitationOnly = phase === "idle" || phase === "clarifying";

  const progressPanel = (
    <AgentProgressPanel
      events={events}
      sseStatus={sseStatus}
      active={running}
      polled={polledAgents}
      scope={scope}
      polledErrors={polledErrors}
      polledRetryable={polledRetryable}
      writing={writing}
      // with a plan on screen, one search can be run again; without one the whole trip is retried (FailedStage)
      onRetry={phase === "complete" ? retrySearch : undefined}
    />
  );

  const sheetTitle = !running
    ? failedSearches.length === 1
      ? `The ${SEARCH_WORDS[failedSearches[0]]} search failed`
      : `${plural(failedSearches.length, "search", "searches")} failed`
    : writing !== undefined
    ? phase === "refining" ? "Writing the updated plan…" : "Writing your itinerary…"
    : phase === "refining"
    ? "Updating your plan…"
    : `Searching — ${searchesDone} of 3 done`;

  // A sheet that is shown again starts closed.
  useEffect(() => {
    if (!showSheet) setSheetOpen(false);
  }, [showSheet]);

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

      {/* the sheet covers the foot of the page while it is shown: leave room under the last thing on it */}
      <main className={`mx-auto max-w-7xl px-4 pt-8 sm:px-6 lg:px-8 lg:pb-12 lg:pt-10 ${showSheet ? "pb-28" : "pb-12"}`}>
        <TripHero trip={trip} phase={phase} />

        <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_380px] lg:items-start">
          {/* ── The plan ───────────────────────────────────────────────── */}
          <div ref={planRef} className={`min-w-0 scroll-mt-20 ${invitationOnly ? "hidden lg:block" : ""}`}>
            {phase === "loading" && (
              <div className="space-y-4" aria-hidden>
                <div className="skeleton h-48" />
                <div className="skeleton h-44" />
              </div>
            )}
            {phase === "idle" && <StartStage destination={destination} onPick={startPlanning} />}
            {phase === "clarifying" && <ClarifyStage question={clarifyQ} />}
            {phase === "planning" && <PlanningStage destination={destination} agents={agentStates} draft={draft} />}
            {phase === "failed" && (
              <FailedStage
                conflict={conflict}
                reason={failureReason}
                failures={failures}
                onReplan={chooseReplan}
                onRetry={retryPlan}
                onPick={startPlanning}
                disabled={busy}
              />
            )}
            {itinerary && refinable && (
              <ItineraryView itinerary={itinerary} trip={trip} updating={phase === "refining" ? updating ?? "pending" : null} changed={changed} />
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

              {/* on a small screen the progress is in the sheet at the foot of the screen instead */}
              {showProgress && wide && <div className="animate-fade border-b border-ink-200/70 px-4 py-3.5">{progressPanel}</div>}

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
              <ChatInput
                onSubmit={handleChatSubmit}
                disabled={running || phase === "loading"}
                loading={sending}
                placeholder={chatPlaceholder}
                // once there is a plan, what is typed here changes it — the box says so before the first word
                label={refinable ? "Refine this trip:" : undefined}
              />
              <p className="mt-1.5 hidden text-center text-[11px] text-ink-500 lg:block">Enter to send · Shift + Enter for a new line</p>
            </div>
          </aside>
        </div>
      </main>

      {/* Small screens: what is pinned to the foot of the screen, stacked so that one never covers the other —
          the way back to the composer, then the planning progress (Phase 20). */}
      {(showSheet || showComposerButton) && (
        <div className="pointer-events-none fixed inset-x-0 bottom-0 z-30 flex flex-col items-center gap-3 px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] lg:hidden">
          {showComposerButton && (
            // centred: the right edge of the day cards is where their "Map" buttons are
            <button
              onClick={focusComposer}
              className="focus-ring pointer-events-auto inline-flex animate-rise items-center gap-2 rounded-full bg-ink-900 py-3 pl-3.5 pr-4
                         text-sm font-medium text-white shadow-float transition active:scale-95"
            >
              <Sparkles className="h-4 w-4 text-saffron" aria-hidden />
              Ask for a change
            </button>
          )}
          {showSheet && (
            <ProgressSheet title={sheetTitle} tone={running ? "busy" : "alert"} open={sheetOpen} onToggle={() => setSheetOpen((open) => !open)}>
              {progressPanel}
            </ProgressSheet>
          )}
        </div>
      )}
    </div>
  );
}
