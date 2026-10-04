/**
 * Phase 22 — the itinerary's local tips, as the page shows them.
 *
 * `structured_data.local_intelligence` is what the DestinationIntelligenceAgent
 * knew about the destination: how to get around, what is expected of a visitor,
 * what to avoid, when each place is at its best, what to watch out for. It
 * comes from a model's own knowledge, so the page shows it as advice to check
 * locally — and reads it defensively: an itinerary saved before there were
 * tips has none, and anything that is not text is left out.
 *
 * Pure functions, no React.
 */

import { FREE_TIME, SLOTS, SLOT_LABELS } from "./map";
import type { DaySchedule } from "./types";

export type TipSectionKey = "local_transport" | "cultural_norms" | "tourist_traps" | "best_times" | "safety_tips";

// The sections, in the order they are shown. The PDF prints the same headings
// (src/backend/app/pdf/plan.py — a test keeps the two in step).
export const TIP_SECTIONS: { key: TipSectionKey; label: string }[] = [
  { key: "local_transport", label: "Getting around" },
  { key: "cultural_norms", label: "Local customs" },
  { key: "tourist_traps", label: "Tourist traps" },
  { key: "best_times", label: "Best times to visit" },
  { key: "safety_tips", label: "Staying safe" },
];

export interface Tip {
  text: string;
  /** best_times: the place the tip is about. */
  place?: string;
  /** best_times: where that place is in the plan — "Day 2 · Morning" — when it is one of its stops. */
  inPlan?: string;
}

export interface TipSection {
  key: TipSectionKey;
  label: string;
  tips: Tip[];
}

const text = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value.trim() : null);

const texts = (value: unknown): string[] =>
  (Array.isArray(value) ? value : []).map(text).filter((item): item is string => item !== null);

/** A place's name as a bag of words: "Aguada Fort" and "Fort Aguada, Goa" name the same place as "Fort Aguada". */
function words(name: string): string[] {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "") // accents on Latin letters only — a vowel sign is part of a Devanagari word
    .toLowerCase()
    .split(/[\s,.;:!?'"’()[\]{}/\\\-–—_&]+/)
    .filter(Boolean);
}

/** Whether two names are the same place: the same words, or all the words of one (two at least) in the other. */
export function samePlace(a: string, b: string): boolean {
  const [short, long] = [words(a), words(b)].sort((x, y) => x.length - y.length);
  if (short.length === 0) return false;
  if (short.length === 1 && long.length > 1) return false; // "Fort" is not "Fort Aguada"
  return short.every((word) => long.includes(word));
}

/** Where a place is in the plan — "Day 2 · Morning" — or undefined when it is not one of its stops. */
export function whereInPlan(place: string, days: DaySchedule[]): string | undefined {
  for (const day of days) {
    for (const slot of SLOTS) {
      const stop = day[slot]?.activity;
      if (stop && stop !== FREE_TIME && samePlace(place, stop)) return `Day ${day.day} · ${SLOT_LABELS[slot]}`;
    }
  }
  return undefined;
}

/** The sections that have something to say, in order. Empty when the itinerary has no tips. */
export function tipSections(raw: unknown, days: DaySchedule[] = []): TipSection[] {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return [];
  const intelligence = raw as Record<string, unknown>;

  const bestTimes = intelligence.best_times;
  const places: Tip[] = Object.entries(typeof bestTimes === "object" && bestTimes !== null && !Array.isArray(bestTimes) ? bestTimes : {})
    .map(([place, when]): Tip | null => {
      const name = text(place);
      const advice = text(when);
      return name && advice ? { text: advice, place: name, inPlan: whereInPlan(name, days) } : null;
    })
    .filter((tip): tip is Tip => tip !== null)
    // the places the traveller is going to come first; otherwise the order they were given in
    .sort((a, b) => Number(Boolean(b.inPlan)) - Number(Boolean(a.inPlan)));

  const transport = text(intelligence.local_transport);
  const tips: Record<TipSectionKey, Tip[]> = {
    local_transport: transport ? [{ text: transport }] : [],
    cultural_norms: texts(intelligence.cultural_norms).map((tip) => ({ text: tip })),
    tourist_traps: texts(intelligence.tourist_traps).map((tip) => ({ text: tip })),
    best_times: places,
    safety_tips: texts(intelligence.safety_tips).map((tip) => ({ text: tip })),
  };
  return TIP_SECTIONS.map((section) => ({ ...section, tips: tips[section.key] })).filter((section) => section.tips.length > 0);
}

/** Whether an itinerary carries any local tips at all. */
export function hasTips(raw: unknown): boolean {
  return tipSections(raw).length > 0;
}
