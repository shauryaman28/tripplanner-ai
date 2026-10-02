/**
 * What a change request actually changed, in the traveller's words.
 *
 * The page holds the itinerary from before the request and the one that came
 * back; this compares the two field by field. No model is involved, so the
 * summary cannot claim a change that did not happen.
 */

import { formatDateRange, formatINR, plural } from "./format";
import { FREE_TIME, SLOTS, SLOT_LABELS } from "./map";
import type { DaySchedule, FlightInfo, HotelSlot, StructuredItinerary, Trip } from "./types";

export interface ChangeSummary {
  /** False when the plan came back the same in every way a traveller would notice. */
  changed: boolean;
  /** One sentence: the new total and how it moved. */
  headline: string;
  /** One line per thing that changed, most consequential first. */
  points: string[];
}

/** "A", "A and B", "A, B and C", "A, B, C and 2 more" */
function listNames(names: string[], max: number = 3): string {
  const shown = names.slice(0, max);
  const rest = names.length - shown.length;
  if (rest > 0) return `${shown.join(", ")} and ${rest} more`;
  if (shown.length <= 1) return shown.join("");
  return `${shown.slice(0, -1).join(", ")} and ${shown[shown.length - 1]}`;
}

/** Every real stop (free time is not one) → where it sits: "day 2, afternoon". */
function placements(days: DaySchedule[]): Map<string, string> {
  const where = new Map<string, string>();
  for (const day of days) {
    for (const slot of SLOTS) {
      const name = day[slot]?.activity;
      if (name && name !== FREE_TIME && !where.has(name)) where.set(name, `day ${day.day}, ${SLOT_LABELS[slot].toLowerCase()}`);
    }
  }
  return where;
}

const firstHotel = (days: DaySchedule[]): HotelSlot | null => days.find((d) => d.hotel)?.hotel ?? null;
const firstFlight = (days: DaySchedule[]): FlightInfo | null => days.find((d) => d.flight)?.flight ?? null;
const flightLabel = (flight: FlightInfo): string => flight.flight_number ?? flight.airline ?? "flight";
const perNight = (hotel: HotelSlot): string => `${formatINR(hotel.cost_per_night)} a night`;

function stayChange(before: HotelSlot | null, after: HotelSlot | null): string | null {
  if (!before && !after) return null;
  if (!before && after) return `Stay added: ${after.name}, ${perNight(after)}`;
  if (before && !after) return `Stay removed: ${before.name}`;
  if (!before || !after) return null;

  const samePrice = Math.round(before.cost_per_night) === Math.round(after.cost_per_night);
  if (before.name !== after.name) {
    const price = samePrice ? `same price, ${perNight(after)}` : `${formatINR(before.cost_per_night)} → ${perNight(after)}`;
    return `Stay: ${before.name} → ${after.name} (${price})`;
  }
  return samePrice ? null : `Stay: ${after.name} is now ${perNight(after)} (was ${formatINR(before.cost_per_night)})`;
}

function flightChange(before: FlightInfo | null, after: FlightInfo | null): string | null {
  if (!before && !after) return null;
  const price = (flight: FlightInfo) => (flight.price_inr != null ? formatINR(flight.price_inr) : "price not listed");
  if (!before && after) return `Flight added: ${flightLabel(after)}, ${price(after)}`;
  if (before && !after) return `Flight removed: ${flightLabel(before)}`;
  if (!before || !after) return null;

  const samePrice = Math.round(before.price_inr ?? 0) === Math.round(after.price_inr ?? 0);
  const sameFlight = flightLabel(before) === flightLabel(after) && before.departure === after.departure;
  if (!sameFlight) {
    return `Flight: ${flightLabel(before)} → ${flightLabel(after)} (${samePrice ? `same price, ${price(after)}` : `${price(before)} → ${price(after)}`})`;
  }
  return samePrice ? null : `Flight: ${flightLabel(after)} is now ${price(after)} (was ${price(before)})`;
}

function placeChanges(before: DaySchedule[], after: DaySchedule[]): string[] {
  const was = placements(before);
  const now = placements(after);
  const added = Array.from(now.keys()).filter((name) => !was.has(name));
  const removed = Array.from(was.keys()).filter((name) => !now.has(name));
  const moved = Array.from(now.keys()).filter((name) => was.has(name) && was.get(name) !== now.get(name));

  const points: string[] = [];
  if (added.length) points.push(`Added: ${listNames(added)}`);
  if (removed.length) points.push(`Removed: ${listNames(removed)}`);
  if (moved.length === 1) points.push(`Moved: ${moved[0]} is now on ${now.get(moved[0])}`);
  else if (moved.length > 1) points.push(`Rearranged: ${plural(moved.length, "stop")} moved to a different day or time`);
  return points;
}

function dateChange(before: DaySchedule[], after: DaySchedule[]): string | null {
  if (!before.length || !after.length) return null;
  const range = (days: DaySchedule[]) => formatDateRange(days[0].date, days[days.length - 1].date);
  if (before.length !== after.length) {
    return `Length: ${plural(before.length, "day")} → ${plural(after.length, "day")} (${range(after)})`;
  }
  return range(before) === range(after) ? null : `Dates: ${range(before)} → ${range(after)}`;
}

/**
 * Compare the itinerary before a change request with the one after it.
 * `trips` adds what lives on the trip rather than the itinerary (the destination).
 */
export function describeChanges(
  before: StructuredItinerary,
  after: StructuredItinerary,
  trips?: { before: Trip | null; after: Trip | null },
): ChangeSummary {
  const points: string[] = [];
  if (trips?.before && trips.after && trips.before.destination !== trips.after.destination) {
    points.push(`Destination: ${trips.before.destination} → ${trips.after.destination}`);
  }
  for (const point of [
    dateChange(before.days, after.days),
    flightChange(firstFlight(before.days), firstFlight(after.days)),
    stayChange(firstHotel(before.days), firstHotel(after.days)),
    ...placeChanges(before.days, after.days),
  ]) {
    if (point) points.push(point);
  }

  const difference = Math.round(after.total_cost) - Math.round(before.total_cost);
  const total = formatINR(after.total_cost);
  const headline =
    difference === 0
      ? `The total stays at ${total}.`
      : `The total is now ${total} — ${formatINR(Math.abs(difference))} ${difference > 0 ? "more" : "less"} than before.`;

  return { changed: points.length > 0 || difference !== 0, headline, points };
}
