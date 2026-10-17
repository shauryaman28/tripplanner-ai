/**
 * Phase 37 — a stop's clock: when it starts and ends, and the way there.
 *
 * Set by the planner (src/ai/schedule.py) on every itinerary made since Phase 37: each stop's
 * `start_time` and `end_time` ("HH:MM"), the minutes from where the traveller was, and — when that
 * was more than 10 km away — a travel note: from where, how long, how far, by what. The time is
 * Google's (its Routes API), or an estimate from the straight line when Google could not be asked;
 * an estimate is said as one ("about 35 min"). An itinerary made before has none of it, and the
 * page shows what it always did.
 */

import type { ActivitySlot, TravelNote } from "./types";

export const TRAVEL_MODES: Record<TravelNote["mode"], string> = {
  walk: "Walk",
  "auto-rickshaw": "Auto-rickshaw",
  taxi: "Taxi",
};

/** "09:05–11:05" — or null for a stop with no times (an older itinerary, or a stop the day had no time left for). */
export function stopTimes(activity: ActivitySlot): string | null {
  return activity.start_time && activity.end_time ? `${activity.start_time}–${activity.end_time}` : null;
}

/** "45 min", "1 h", "1 h 20 min". */
export function formatMinutes(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} min`;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

/** "Taxi · 46 min · 18.9 km from the airport" — "about 46 min" when it is an estimate, not Google's. */
export function travelWords(note: TravelNote): string {
  const time = `${note.source === "google" ? "" : "about "}${formatMinutes(note.minutes)}`;
  const far = note.km != null ? ` · ${note.km.toLocaleString("en-IN", { maximumFractionDigits: 1 })} km` : "";
  return `${TRAVEL_MODES[note.mode] ?? "Taxi"} · ${time}${far} from ${note.from}`;
}
