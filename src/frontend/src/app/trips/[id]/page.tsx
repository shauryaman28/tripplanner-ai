"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  clarifyTrip,
  getItinerary,
  getToken,
  planTrip,
  refineTrip,
} from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type { Itinerary, Trip, SSEAgentUpdateEvent } from "@/lib/types";
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

type PlanningPhase =
  | "idle"          // trip exists, nothing started
  | "planning"      // SSE open, agents running
  | "clarifying"    // backend returned clarification_needed
  | "complete"      // itinerary ready
  | "failed"        // terminal error
  | "refining";     // targeted refinement in progress

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

  // ── Auth guard ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!getToken()) router.replace("/login");
  }, [router]);

  // ── Trip metadata (loaded once from local state passed via sessionStorage
  //    or derived from the SSE / itinerary response) ─────────────────────
  const [destination, setDestination] = useState<string>("…");
  const [phase, setPhase]             = useState<PlanningPhase>("idle");
  const [messages, setMessages]       = useState<Message[]>([]);
  const [itinerary, setItinerary]     = useState<Itinerary | null>(null);
  const [loading, setLoading]         = useState(false);
  const [clarifyQ, setClarifyQ]       = useState<string | null>(null);

  // Track whether SSE should be open
  const [sseEnabled, setSseEnabled]   = useState(false);

  // ── SSE ────────────────────────────────────────────────────────────────
  const { events, status: sseStatus, close: closeSSE } = useSSE(tripId, sseEnabled);

  // ── Ref to avoid stale closure in effect below ─────────────────────────
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  // ── React to SSE events ────────────────────────────────────────────────
  useEffect(() => {
    if (events.length === 0) return;
    const last = events[events.length - 1] as SSEAgentUpdateEvent;

    if (last.event === "planning_complete") {
      // Fetch the real itinerary from the API
      setPhase("complete");
      setSseEnabled(false);
      closeSSE();
      getItinerary(tripId)
        .then(setItinerary)
        .catch(() => {
          addMessage("assistant", "Planning finished but I couldn't load the itinerary. Please refresh.");
        });
    }

    if (last.event === "planning_failed") {
      setPhase("failed");
      setSseEnabled(false);
      closeSSE();
      addMessage("assistant", `Planning failed: ${last.reason ?? "unknown error"}. You can try again.`);
    }

    if (last.event === "budget_conflict") {
      addMessage(
        "assistant",
        `⚠️ Budget conflict: ${last.reason ?? ""}\n\nOptions:\n${(last.options ?? []).map((o) => `• ${o.description} (${o.estimated_saving})`).join("\n")}`,
      );
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events]);

  // ── Load destination label from trips list (stored in sessionStorage) ──
  useEffect(() => {
    try {
      const cached = sessionStorage.getItem("tp_trips");
      if (cached) {
        const trips: Trip[] = JSON.parse(cached);
        const t = trips.find((x) => x.id === tripId);
        if (t) setDestination(t.destination);
      }
    } catch {
      // ignore
    }
  }, [tripId]);

  // ── Helpers ────────────────────────────────────────────────────────────

  function addMessage(role: Message["role"], text: string) {
    setMessages((prev) => [...prev, { role, text, id: uid() }]);
  }

  // ── Start planning ──────────────────────────────────────────────────────

  const startPlanning = useCallback(
    async (rawInput: string) => {
      addMessage("user", rawInput);
      setLoading(true);
      setPhase("planning");
      setSseEnabled(true);

      try {
        const res = await planTrip(tripId, { raw_input: rawInput });

        if ((res as unknown as { status: string }).status === "clarification_needed") {
          const q = (res as unknown as { question: string }).question;
          setClarifyQ(q);
          setPhase("clarifying");
          setSseEnabled(false);
          addMessage("assistant", q);
        } else {
          addMessage("assistant", "On it! I'm searching for flights, hotels, and activities…");
        }
      } catch (err: unknown) {
        setPhase("failed");
        setSseEnabled(false);
        addMessage("assistant", `Something went wrong: ${err instanceof Error ? err.message : "unknown error"}`);
      } finally {
        setLoading(false);
      }
    },
    [tripId],
  );

  // ── Answer clarification ────────────────────────────────────────────────

  const answerClarification = useCallback(
    async (answer: string) => {
      addMessage("user", answer);
      setClarifyQ(null);
      setLoading(true);
      setPhase("planning");
      setSseEnabled(true);

      try {
        await clarifyTrip(tripId, answer);
        addMessage("assistant", "Got it, searching now…");
      } catch (err: unknown) {
        setPhase("failed");
        setSseEnabled(false);
        addMessage("assistant", `Clarification failed: ${err instanceof Error ? err.message : "unknown"}`);
      } finally {
        setLoading(false);
      }
    },
    [tripId],
  );

  // ── Refinement ─────────────────────────────────────────────────────────

  const submitRefinement = useCallback(
    async (message: string) => {
      addMessage("user", message);
      setLoading(true);
      setPhase("refining");
      setSseEnabled(true);

      try {
        const res = await refineTrip(tripId, message);
        addMessage("assistant", `Refining your trip (${res.refinement_type.replace(/_/g, " ")})…`);
      } catch (err: unknown) {
        setPhase("complete"); // fall back to showing existing itinerary
        setSseEnabled(false);
        addMessage("assistant", `Refinement failed: ${err instanceof Error ? err.message : "unknown"}`);
      } finally {
        setLoading(false);
      }
    },
    [tripId],
  );

  // ── Route input to the right handler ───────────────────────────────────

  function handleChatSubmit(text: string) {
    if (phase === "clarifying") {
      answerClarification(text);
    } else if (phase === "complete" || phase === "failed") {
      submitRefinement(text);
    } else if (phase === "idle") {
      startPlanning(text);
    }
  }

  // Determine chat input props
  const chatDisabled = phase === "planning" || phase === "refining";
  const chatPlaceholder =
    phase === "clarifying"
      ? `${clarifyQ ?? "Answer the question above…"}`
      : phase === "complete"
      ? "Refine your trip — e.g. change hotels, make it cheaper…"
      : phase === "idle"
      ? "Tell me about your trip…"
      : "Planning in progress…";

  return (
    <div className="flex min-h-dvh flex-col bg-surface">
      <NavBar destination={destination} onBack={() => router.push("/trips")} />

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
              loading={loading}
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
            <AgentProgressPanel events={events} sseStatus={sseStatus} />
          </div>
        </aside>
      </div>
    </div>
  );
}
