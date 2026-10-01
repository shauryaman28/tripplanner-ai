"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  clarifyTrip,
  getItinerary,
  getToken,
  getTrip,
  getTripStatus,
  planTrip,
  refineTrip,
  replanTrip,
} from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { AgentStatus, BudgetConflictOption, Itinerary, Trip } from "@/lib/types";
import AgentProgressPanel from "@/components/AgentProgressPanel";
import ItineraryView from "@/components/ItineraryView";
import ChatInput from "@/components/ChatInput";
import MessageThread, { type Message } from "@/components/MessageThread";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function uid() {
  return Math.random().toString(36).slice(2);
}

function errorText(err: unknown) {
  return err instanceof Error ? err.message : "unknown error";
}

type PlanningPhase =
  | "loading"       // fetching the trip
  | "idle"          // trip exists, nothing started
  | "planning"      // agents running
  | "clarifying"    // backend returned clarification_needed
  | "complete"      // itinerary ready
  | "failed"        // last run failed and there is no itinerary to show
  | "refining";     // refinement in progress (previous itinerary stays visible)

const POLL_MS = 3_000;

// ---------------------------------------------------------------------------
// Nav header
// ---------------------------------------------------------------------------

function NavBar({
  destination,
  onBack,
}: {
  destination: string;
  onBack: () => void;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-gray-100 bg-white/90 backdrop-blur-sm">
      <div className="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-muted hover:text-gray-700 focus-ring rounded"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
            <path fillRule="evenodd" d="M17 10a.75.75 0 01-.75.75H5.612l4.158 3.96a.75.75 0 11-1.04 1.08l-5.5-5.25a.75.75 0 010-1.08l5.5-5.25a.75.75 0 111.04 1.08L5.612 9.25H16.25A.75.75 0 0117 10z" />
          </svg>
          Trips
        </button>
        <span className="text-gray-300">/</span>
        <h1 className="font-display text-sm font-semibold text-gray-900 truncate">
          {destination}
        </h1>
      </div>
    </header>
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
  const [conflict, setConflict]         = useState<BudgetConflictOption[] | null>(null);
  const [polledAgents, setPolledAgents] = useState<Record<string, AgentStatus>>({});

  const running = phase === "planning" || phase === "refining";

  // The stream stays open for the life of the page, so it is already subscribed
  // when a run starts — pub/sub has no replay for late subscribers.
  const { events, status: sseStatus, reset: resetEvents } = useSSE(tripId, phase !== "loading");

  // Refs so the stable callbacks below always see current values.
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const itineraryRef = useRef(itinerary);
  itineraryRef.current = itinerary;
  // True while a run's outcome is still owed; SSE and polling race to report it.
  const awaitingOutcome = useRef(false);

  const addMessage = useCallback((role: Message["role"], text: string) => {
    setMessages((prev) => [...prev, { role, text, id: uid() }]);
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
        const existing = await getItinerary(tripId).catch(() => null);
        if (cancelled) return;
        setTrip(loaded);
        setItinerary(existing);
        if (loaded.status === "planning") {
          awaitingOutcome.current = true;
          setPhase(existing ? "refining" : "planning");
        } else if (existing) {
          setPhase("complete");
        } else if (loaded.status === "failed") {
          setPhase("failed");
          addMessage("assistant", "The last planning attempt failed. Describe your trip to try again.");
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

  // ── A run ended. SSE and polling can both report it — handle it once. ──
  const finishRun = useCallback(
    async (succeeded: boolean, error?: string) => {
      if (!awaitingOutcome.current) return;
      awaitingOutcome.current = false; // claim it before any await

      if (succeeded) {
        try {
          const [latest, refreshed] = await Promise.all([getItinerary(tripId), getTrip(tripId)]);
          setItinerary(latest);
          setTrip(refreshed); // a refinement may have moved the dates or the destination
          setPhase("complete");
          return;
        } catch {
          error = "the itinerary could not be loaded";
        }
      }
      if (error) addMessage("assistant", `Planning failed: ${error}.`);
      setPhase(itineraryRef.current ? "complete" : "failed");
    },
    [tripId, addMessage],
  );

  // ── React to every new SSE event (several can arrive in one render) ────
  const handled = useRef(0);
  useEffect(() => {
    for (const ev of events.slice(Math.min(handled.current, events.length))) {
      if (ev.event === "budget_conflict") {
        setConflict(ev.options ?? []);
        addMessage("assistant", `⚠️ ${ev.reason ?? "Flights exceed the budget."} Pick an option below, or describe a different trip.`);
      } else if (ev.event === "planning_complete") {
        finishRun(true);
      } else if (ev.event === "planning_failed") {
        // a budget conflict has already explained itself in the message above
        finishRun(false, events.some((e) => e.event === "budget_conflict") ? undefined : ev.error ?? "unknown error");
      }
    }
    handled.current = events.length;
  }, [events, addMessage, finishRun]);

  // ── Polling fallback: catches a run that ended while the stream was down ─
  useEffect(() => {
    if (!running || sending) return;
    const timer = setInterval(async () => {
      try {
        const current = await getTripStatus(tripId);
        setPolledAgents(current.progress.agents);
        if (current.status === "completed") finishRun(true);
        else if (current.status === "failed") finishRun(false, "see the planning progress panel for details");
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
      resetEvents();
      setConflict(null);
      setPolledAgents({});
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
        return "On it! I'm searching for flights, hotels, and activities…";
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
        return `Refining your trip (${res.refinement_type.replace(/_/g, " ")})…`;
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

  // Determine chat input props
  const chatDisabled = running || phase === "loading";
  const chatPlaceholder =
    phase === "clarifying"
      ? `${clarifyQ ?? "Answer the question above…"}`
      : phase === "complete"
      ? "Refine your trip — e.g. change hotels, make it cheaper…"
      : phase === "idle" || phase === "failed"
      ? "Tell me about your trip…"
      : "Planning in progress…";

  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      <NavBar destination={trip?.destination ?? "…"} onBack={() => router.push("/trips")} />

      {/* Body: two-column on lg+ */}
      <div className="mx-auto flex w-full max-w-6xl flex-1 gap-6 px-4 py-6 lg:flex-row flex-col">

        {/* ── Left: Chat + Itinerary ─────────────────────────────────── */}
        <div className="flex flex-1 flex-col gap-5 min-w-0">

          {/* Chat thread */}
          {messages.length > 0 && (
            <div className="space-y-3">
              <MessageThread messages={messages} />
            </div>
          )}

          {/* Empty state when idle */}
          {phase === "idle" && messages.length === 0 && (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 rounded-2xl border border-dashed border-gray-200 bg-white py-16 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-accent-500 shadow-lg">
                <svg className="h-7 w-7 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              </div>
              <div>
                <p className="font-display text-lg font-semibold text-gray-900">Ready to plan</p>
                <p className="mt-1 text-sm text-muted max-w-xs mx-auto">
                  Describe your ideal trip below and I'll find flights, hotels, and activities.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-2 mt-1">
                {[
                  "7 days in Goa, December, budget ₹50,000",
                  "Weekend in Jaipur, 2 people",
                  "5 nights Kerala backwaters, ₹80,000",
                ].map((hint) => (
                  <button
                    key={hint}
                    onClick={() => startPlanning(hint)}
                    className="rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600
                               hover:border-brand-300 hover:text-brand-700 transition-colors focus-ring"
                  >
                    {hint}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Budget conflict → POST /trips/{id}/replan */}
          {conflict && phase === "failed" && (
            <div className="flex flex-wrap gap-2" aria-label="Budget options">
              {conflict.map((option) => (
                <button
                  key={option.choice}
                  onClick={() => chooseReplan(option)}
                  className="rounded-full border border-orange-200 bg-orange-50 px-3 py-1.5 text-xs text-orange-700
                             hover:border-orange-300 hover:bg-orange-100 transition-colors focus-ring"
                >
                  {option.description} ({option.estimated_saving})
                </button>
              ))}
            </div>
          )}

          {/* Itinerary */}
          {itinerary && phase === "complete" && (
            <ItineraryView itinerary={itinerary} />
          )}

          {/* Refining spinner overlay on top of existing itinerary */}
          {phase === "refining" && itinerary && (
            <>
              <div className="rounded-xl border border-brand-100 bg-brand-50 px-4 py-3">
                <p className="text-sm font-medium text-brand-700">
                  Updating your itinerary…
                </p>
              </div>
              <ItineraryView itinerary={itinerary} />
            </>
          )}

          {/* Chat input — pinned at bottom of left column */}
          <div className="sticky bottom-4 mt-auto">
            <ChatInput
              onSubmit={handleChatSubmit}
              disabled={chatDisabled}
              loading={sending}
              placeholder={chatPlaceholder}
            />
            <p className="mt-1.5 text-center text-xs text-muted">
              {phase === "complete"
                ? "Itinerary ready — ask to refine it anytime"
                : "Enter to send · Shift+Enter for new line"}
            </p>
          </div>
        </div>

        {/* ── Right: Agent progress panel ──────────────────────────── */}
        <aside className="w-full lg:w-72 shrink-0">
          <div className="sticky top-20">
            <AgentProgressPanel events={events} sseStatus={sseStatus} active={running} polled={polledAgents} />
          </div>
        </aside>
      </div>
    </div>
  );
}
