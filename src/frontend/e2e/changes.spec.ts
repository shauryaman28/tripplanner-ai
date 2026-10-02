/**
 * describeChanges() — what the assistant says after a change request.
 *
 * Pure function tests (no browser): they run in the Playwright runner because
 * it is the frontend's only test runner.
 */

import { expect, test } from "@playwright/test";

import { describeChanges } from "../src/lib/changes";
import type { ActivitySlot, DaySchedule, FlightInfo, HotelSlot, StructuredItinerary } from "../src/lib/types";

const stop = (activity: string): ActivitySlot => ({ activity, cost: 0 });
const HOTEL: HotelSlot = { name: "Old Inn", cost_per_night: 3000 };
const FLIGHT: FlightInfo = { flight_number: "6E-204", departure: "2026-12-10T06:00:00", price_inr: 8200 };

function day(n: number, slots: Partial<Pick<DaySchedule, "morning" | "afternoon" | "evening">>, extra: Partial<DaySchedule> = {}): DaySchedule {
  return { day: n, date: `2026-12-${9 + n}`, morning: null, afternoon: null, evening: null, hotel: HOTEL, flight: null, ...slots, ...extra };
}

function plan(days: DaySchedule[], total_cost: number = 14200): StructuredItinerary {
  return { days, total_cost, currency: "INR" };
}

const BEFORE = plan([
  day(1, { morning: stop("Fort Aguada"), afternoon: stop("Baga Beach") }, { flight: FLIGHT }),
  day(2, { morning: stop("Basilica of Bom Jesus") }, { hotel: null }),
]);

test("the same plan is reported as unchanged", () => {
  const summary = describeChanges(BEFORE, structuredClone(BEFORE));
  expect(summary.changed).toBe(false);
  expect(summary.points).toEqual([]);
});

test("a different hotel: names, nightly prices and the new total", () => {
  const nicer: HotelSlot = { name: "Beach House", cost_per_night: 6000 };
  const after = plan([{ ...BEFORE.days[0], hotel: nicer }, BEFORE.days[1]], 17200);
  const summary = describeChanges(BEFORE, after);
  expect(summary.points).toEqual(["Stay: Old Inn → Beach House (₹3,000 → ₹6,000 a night)"]);
  expect(summary.headline).toBe("The total is now ₹17,200 — ₹3,000 more than before.");
});

test("a different hotel at the same price says so", () => {
  const after = plan([{ ...BEFORE.days[0], hotel: { name: "Beach House", cost_per_night: 3000 } }, BEFORE.days[1]]);
  const summary = describeChanges(BEFORE, after);
  expect(summary.points).toEqual(["Stay: Old Inn → Beach House (same price, ₹3,000 a night)"]);
  expect(summary.headline).toBe("The total stays at ₹14,200.");
});

test("a cheaper flight", () => {
  const cheaper: FlightInfo = { flight_number: "AI-433", departure: "2026-12-10T09:00:00", price_inr: 6100 };
  const after = plan([{ ...BEFORE.days[0], flight: cheaper }, BEFORE.days[1]], 12100);
  const summary = describeChanges(BEFORE, after);
  expect(summary.points).toEqual(["Flight: 6E-204 → AI-433 (₹8,200 → ₹6,100)"]);
  expect(summary.headline).toBe("The total is now ₹12,100 — ₹2,100 less than before.");
});

test("places swapped in and out, and one moved", () => {
  const after = plan([
    day(1, { morning: stop("Fort Aguada"), afternoon: stop("Reis Magos Fort") }, { flight: FLIGHT }),
    day(2, { morning: stop("Se Cathedral"), afternoon: stop("Basilica of Bom Jesus") }, { hotel: null }),
  ]);
  expect(describeChanges(BEFORE, after).points).toEqual([
    "Added: Reis Magos Fort and Se Cathedral",
    "Removed: Baga Beach",
    "Moved: Basilica of Bom Jesus is now on day 2, afternoon",
  ]);
});

test("free time is not a place: dropping it is not a change", () => {
  const padded = plan([BEFORE.days[0], { ...BEFORE.days[1], afternoon: stop("Explore the area") }]);
  expect(describeChanges(padded, BEFORE).changed).toBe(false);
});

test("a longer trip: the new length and dates, and long lists are cut short", () => {
  const after = plan(
    [
      ...BEFORE.days.slice(0, 1),
      day(2, { morning: stop("Basilica of Bom Jesus") }),
      day(3, { morning: stop("A"), afternoon: stop("B"), evening: stop("C") }),
      day(4, { morning: stop("D") }, { hotel: null }),
    ],
    20200,
  );
  expect(describeChanges(BEFORE, after).points).toEqual([
    "Length: 2 days → 4 days (10 – 13 Dec 2026)",
    "Added: A, B, C and 1 more",
  ]);
});

test("a new destination comes first", () => {
  const trip = (destination: string) => ({ destination }) as never;
  const after = plan([day(1, { morning: stop("Gateway of India") }, { flight: FLIGHT }), day(2, {}, { hotel: null })]);
  const summary = describeChanges(BEFORE, after, { before: trip("Goa"), after: trip("Mumbai") });
  expect(summary.points[0]).toBe("Destination: Goa → Mumbai");
  expect(summary.points).toContain("Added: Gateway of India");
});
