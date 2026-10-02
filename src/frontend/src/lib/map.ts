/**
 * Phase 18 — turns an itinerary into what the map draws.
 *
 * Pure functions, no Leaflet: coordinates come from the API (attached to every
 * slot by the backend), so there is no geocoding here. Anything without
 * coordinates is reported in `unmapped` instead of being drawn.
 */

import type { ActivitySlot, DaySchedule } from "./types";

export const SLOTS = ["morning", "afternoon", "evening"] as const;
export type SlotName = (typeof SLOTS)[number];

export const SLOT_LABELS: Record<SlotName, string> = {
  morning:   "Morning",
  afternoon: "Afternoon",
  evening:   "Evening",
};

/** The builder's free-time slot — not a place, so never a pin and never "missing". */
export const FREE_TIME = "Explore the area";

// ── How a pin looks ─────────────────────────────────────────────────────────

/**
 * One colour per day: day 1 blue, day 2 green, …
 *
 * Any two days' pins can sit side by side on a map, so this palette is
 * validated for ALL pairs, not just neighbours: colour-blind ΔE ≥ 8 (protan and
 * deutan) and normal-vision ΔE ≥ 15 for every pair (DECISIONS.md #81). The
 * order keeps the first days — every short trip — furthest apart.
 */
export const DAY_COLORS = ["#2a78d6", "#1baf7a", "#882255", "#ee7733", "#117733", "#56b4e9", "#4a3aa7"] as const;

/** Past the last colour the hues come round again with a different shape — never a new, weaker hue. */
const PIN_SHAPES = ["circle", "square", "diamond"] as const;
export type PinShape = (typeof PIN_SHAPES)[number];

// White text fails contrast on these fills; they take ink instead.
const LIGHT_FILLS = new Set<string>(["#1baf7a", "#ee7733", "#56b4e9"]);
const INK = "#0b0b0b";

export interface PinStyle {
  color: string;
  /** colour of the label drawn on the fill */
  ink: string;
  shape: PinShape;
}

export function dayStyle(day: number): PinStyle {
  const index = Math.max(Math.trunc(day) || 1, 1) - 1;
  const color = DAY_COLORS[index % DAY_COLORS.length];
  return {
    color,
    ink: LIGHT_FILLS.has(color) ? INK : "#ffffff",
    shape: PIN_SHAPES[Math.floor(index / DAY_COLORS.length) % PIN_SHAPES.length],
  };
}

/** Gold, far enough from the orange day colour to hold up for colour-blind readers too. */
export const HOTEL_STYLE: PinStyle = { color: "#f4c430", ink: INK, shape: "square" };
/** Neutral — an info marker, not part of the trip's days. */
export const AIRPORT_STYLE: PinStyle = { color: "#3a3936", ink: "#ffffff", shape: "circle" };

// ── What the map draws ──────────────────────────────────────────────────────

export type LatLng = [number, number];

/** One time slot of a day, as both the day card and the map see it. */
export interface DayStop {
  /** `${day}-${slot}` — the same id on the card's badge and on the map's marker */
  id: string;
  slot: SlotName;
  activity: ActivitySlot;
  freeTime: boolean;
  position: LatLng | null;
  /** 1-based position among this day's mapped stops — the number on the pin; null when it has no pin */
  order: number | null;
}

export interface ActivityPin {
  id: string;
  position: LatLng;
  day: number;
  slot: SlotName;
  order: number;
  name: string;
  category: string | null;
  rating: number | null;
  cost: number;
}

export interface HotelPin {
  id: string;
  position: LatLng;
  name: string;
  stars: number | null;
  rating: number | null;
  address: string | null;
  costPerNight: number;
}

export interface AirportPin {
  id: string;
  position: LatLng;
  role: "origin" | "destination";
  code: string;
  name: string | null;
}

export interface DayRoute {
  day: number;
  positions: LatLng[];   // morning → afternoon → evening
}

export interface MapData {
  activities: ActivityPin[];
  hotels: HotelPin[];
  airports: AirportPin[];
  routes: DayRoute[];
  /** days that have at least one pin, in order */
  days: number[];
  /** real activities that cannot be pinned, e.g. "Day 2 · Afternoon: Some Place" */
  unmapped: string[];
}

function position(lat?: number | null, lng?: number | null): LatLng | null {
  return typeof lat === "number" && typeof lng === "number" ? [lat, lng] : null;
}

export function hotelPinId(name: string): string {
  return `hotel-${name}`;
}

/**
 * The day's filled slots in visiting order, each with its pin number if it has one.
 *
 * Free time is shown once per day at most: the builder now saves it that way,
 * but itineraries saved earlier repeat it in every spare slot.
 */
export function dayStops(day: DaySchedule): DayStop[] {
  const filled = SLOTS.filter((slot) => day[slot]);
  const real = filled.filter((slot) => day[slot]!.activity !== FREE_TIME);
  const shown = real.length > 0 ? real : filled.slice(0, 1);

  let order = 0;
  return shown.map((slot) => {
    const activity = day[slot]!;
    const freeTime = activity.activity === FREE_TIME;
    const at = freeTime ? null : position(activity.lat, activity.lng);
    return { id: `${day.day}-${slot}`, slot, activity, freeTime, position: at, order: at ? ++order : null };
  });
}

export function buildMapData(days: DaySchedule[]): MapData {
  const data: MapData = { activities: [], hotels: [], airports: [], routes: [], days: [], unmapped: [] };
  const hotelsSeen = new Set<string>();
  const airportsSeen = new Set<string>();

  for (const day of days) {
    const route: LatLng[] = [];

    for (const stop of dayStops(day)) {
      if (stop.freeTime) continue;
      if (!stop.position || stop.order === null) {
        data.unmapped.push(`Day ${day.day} · ${SLOT_LABELS[stop.slot]}: ${stop.activity.activity}`);
        continue;
      }
      route.push(stop.position);
      data.activities.push({
        id: stop.id,
        position: stop.position,
        day: day.day,
        slot: stop.slot,
        order: stop.order,
        name: stop.activity.activity,
        category: stop.activity.category ?? null,
        rating: stop.activity.rating ?? null,
        cost: stop.activity.cost ?? 0,
      });
    }

    if (route.length > 0) data.days.push(day.day);
    if (route.length > 1) data.routes.push({ day: day.day, positions: route });

    const hotelAt = day.hotel ? position(day.hotel.lat, day.hotel.lng) : null;
    if (day.hotel && hotelAt && !hotelsSeen.has(day.hotel.name)) {
      hotelsSeen.add(day.hotel.name);
      data.hotels.push({
        id: hotelPinId(day.hotel.name),
        position: hotelAt,
        name: day.hotel.name,
        stars: day.hotel.stars ?? null,
        rating: day.hotel.rating ?? null,
        address: day.hotel.address ?? null,
        costPerNight: day.hotel.cost_per_night,
      });
    }

    for (const role of ["origin", "destination"] as const) {
      const airport = day.flight?.[role];
      const at = airport ? position(airport.lat, airport.lng) : null;
      if (airport && at && !airportsSeen.has(airport.code)) {
        airportsSeen.add(airport.code);
        data.airports.push({ id: `airport-${airport.code}`, position: at, role, code: airport.code, name: airport.name ?? null });
      }
    }
  }

  return data;
}
