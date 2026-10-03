/**
 * readPartialJson() / readDraft() — the itinerary while it is being written (Phase 20).
 *
 * Pure function tests (no browser): they run in the Playwright runner because
 * it is the frontend's only test runner.
 */

import { expect, test } from "@playwright/test";

import { draftProgress, readDraft, readPartialJson } from "../src/lib/draft";

const slot = (activity: string) => ({ activity, cost: 0, lat: null, lng: null });
const HOTEL = { name: "Goa Grand", cost_per_night: 4500 };

// What the builder writes for a three-day trip: indented JSON, free time spelled "Explore the area".
const REPLY = JSON.stringify(
  {
    days: [
      { day: 1, date: "2027-12-10", morning: slot("Fort Aguada"), afternoon: slot("Baga Beach"), evening: null, hotel: HOTEL, flight: null },
      { day: 2, date: "2027-12-11", morning: slot("Basilica of Bom Jesus"), afternoon: slot("Explore the area"), evening: null, hotel: HOTEL, flight: null },
      { day: 3, date: "2027-12-12", morning: slot("Explore the area"), afternoon: slot("Explore the area"), evening: null, hotel: null, flight: null },
    ],
    total_cost: 17200,
    currency: "INR",
  },
  null,
  1,
);

test.describe("readPartialJson", () => {
  test("nothing to read before the first brace", () => {
    for (const text of ["", "   ", "```json\n", "Here is the plan"]) expect(readPartialJson(text)).toBeNull();
  });

  test("a finished reply is read whole", () => {
    expect(readPartialJson(REPLY)).toEqual({ value: JSON.parse(REPLY), complete: true, cutString: null });
  });

  test("a fence or a word of explanation around the reply is ignored", () => {
    expect(readPartialJson('```json\n{"days": []}\n```')).toEqual({ value: { days: [] }, complete: true, cutString: null });
    expect(readPartialJson('Sure! {"total_cost": 5}')?.value).toEqual({ total_cost: 5 });
  });

  test("an object that is not closed yet is read as far as it goes", () => {
    expect(readPartialJson('{"days": [{"day": 1, "date": "2027-12-10", "morning": {"activity": "Fort Agu')).toEqual({
      value: { days: [{ day: 1, date: "2027-12-10", morning: { activity: "Fort Agu" } }] },
      complete: false,
      cutString: "Fort Agu",
    });
  });

  test("a key that is cut off names nothing yet", () => {
    expect(readPartialJson('{"days": [], "total_co')?.value).toEqual({ days: [] });
    expect(readPartialJson('{"days": [], "total_cost"')?.value).toEqual({ days: [] });
    expect(readPartialJson('{"days": [], "total_cost":')?.value).toEqual({ days: [] });
  });

  test("a number at the very end may have more digits coming", () => {
    expect(readPartialJson('{"total_cost": 172')?.value).toEqual({});
    expect(readPartialJson('{"total_cost": 17200,')?.value).toEqual({ total_cost: 17200 });
    expect(readPartialJson('{"total_cost": 17200}')).toEqual({ value: { total_cost: 17200 }, complete: true, cutString: null });
    expect(readPartialJson('{"lat": -15.49e0, "ok": true, "none": null}')?.value).toEqual({ lat: -15.49, ok: true, none: null });
  });

  test("a literal that is cut off is not a value", () => {
    expect(readPartialJson('{"evening": nul')?.value).toEqual({});
    expect(readPartialJson('{"evening": null')?.value).toEqual({ evening: null }); // unambiguous: nothing can follow "null" but a separator
  });

  test("escapes — also one that is cut off in the middle", () => {
    expect(readPartialJson('{"a": "Tom\\u0027s \\"Inn\\"\\n2nd \\\\ line"}')?.value).toEqual({ a: 'Tom\'s "Inn"\n2nd \\ line' });
    expect(readPartialJson('{"a": "caf\\u00')).toEqual({ value: { a: "caf" }, complete: false, cutString: "caf" });
    expect(readPartialJson('{"a": "caf\\')?.value).toEqual({ a: "caf" });
    expect(readPartialJson('{"a": "\\ud83c\\udfd6"}')?.value).toEqual({ a: "🏖" }); // a surrogate pair
  });

  test("text that is not JSON never throws", () => {
    for (const text of ["{", "{]", '{"days": [}', '{"a" 1}', "{1: 2}", '{"a": tru', '{"a": [1, 2', "{,,,}", '{"a": {"b": [{"c": "'])
      expect(() => readPartialJson(text)).not.toThrow();
    expect(readPartialJson('{"a": [1, 2')?.value).toEqual({ a: [1] });
  });
});

test.describe("readDraft", () => {
  test("every prefix of a reply can be read, and the plan only ever grows", () => {
    let days = 0;
    let lines = 0;
    for (let length = 0; length <= REPLY.length; length++) {
      const draft = readDraft(REPLY.slice(0, length));
      expect(draft.days.length).toBeGreaterThanOrEqual(days);
      days = draft.days.length;
      // a line may be replaced (two free-time slots become one "All day"), but a day never loses lines mid-word
      const total = draft.days.reduce((sum, day) => sum + day.lines.length, 0);
      expect(total).toBeGreaterThanOrEqual(lines - 1);
      lines = total;
      expect(draft.complete).toBe(length === REPLY.length);
    }
  });

  test("the finished draft: places, the stay, free time said once", () => {
    expect(readDraft(REPLY)).toEqual({
      complete: true,
      days: [
        {
          day: 1,
          date: "2027-12-10",
          lines: [
            { label: "Morning", text: "Fort Aguada" },
            { label: "Afternoon", text: "Baga Beach" },
            { label: "Stay", text: "Goa Grand" },
          ],
        },
        // free time beside a real stop is not said at all
        { day: 2, date: "2027-12-11", lines: [{ label: "Morning", text: "Basilica of Bom Jesus" }, { label: "Stay", text: "Goa Grand" }] },
        // a day with nothing else: once, for the whole day
        { day: 3, date: "2027-12-12", lines: [{ label: "All day", text: "Free time" }] },
      ],
    });
  });

  test("a place appears letter by letter", () => {
    const upTo = (words: string) => readDraft(REPLY.slice(0, REPLY.indexOf(words) + words.length));
    expect(upTo("Fort Agu").days).toEqual([{ day: 1, date: "2027-12-10", lines: [{ label: "Morning", text: "Fort Agu" }] }]);
    expect(upTo("Fort Aguada").days[0].lines).toEqual([{ label: "Morning", text: "Fort Aguada" }]);
    expect(upTo("Baga B").days[0].lines[1]).toEqual({ label: "Afternoon", text: "Baga B" });
  });

  test("a date is shown once all of it has arrived; a day without a number is counted", () => {
    expect(readDraft('{"days": [{"day": 1, "date": "2027-12').days).toEqual([{ day: 1, date: null, lines: [] }]);
    expect(readDraft('{"days": [{"date": "2027-12-10"}, {"date": "soon"').days).toEqual([
      { day: 1, date: "2027-12-10", lines: [] },
      { day: 2, date: null, lines: [] },
    ]);
  });

  test("words that could still become free time wait until it is clear what they are", () => {
    const day = (activity: string) => readDraft(`{"days": [{"day": 1, "morning": {"activity": "${activity}`).days[0].lines;
    expect(day("")).toEqual([]);
    expect(day("Explore the ar")).toEqual([]); // not shown as a place called "Explore the ar"
    expect(day("Explore the area")).toEqual([{ label: "All day", text: "Free time" }]);
    expect(day("Explorers' Club")).toEqual([{ label: "Morning", text: "Explorers' Club" }]); // a real place after all
    expect(day("Elephanta Caves")).toEqual([{ label: "Morning", text: "Elephanta Caves" }]);
  });

  test("anything that is not an itinerary reads as an empty draft", () => {
    for (const text of ["", "not json", "[1, 2]", '{"days": "soon"}', '{"days": [1, "x", null]}', '{"total_cost": 5}']) {
      expect(readDraft(text).days).toEqual([]);
    }
    // a slot that is a string, or has no activity yet, is simply not a line
    expect(readDraft('{"days": [{"day": 1, "morning": "beach", "afternoon": {"cost": 0}, "hotel": {"cost_per_night": 1}}]}').days).toEqual([
      { day: 1, date: null, lines: [] },
    ]);
  });
});

test("draftProgress: where the writing has got to, in one line", () => {
  const upTo = (words: string) => draftProgress(readDraft(REPLY.slice(0, REPLY.indexOf(words) + words.length)));
  expect(draftProgress(readDraft(""))).toBeNull();
  expect(upTo('"day": 1,')).toBe("Day 1");
  expect(upTo("Fort Agu")).toBe("Day 1 · Morning — Fort Agu");
  expect(upTo("Basilica of Bom Jesus")).toBe("Day 2 · Morning — Basilica of Bom Jesus");
  expect(draftProgress(readDraft(REPLY))).toBe("Day 3 · All day — Free time");
});
