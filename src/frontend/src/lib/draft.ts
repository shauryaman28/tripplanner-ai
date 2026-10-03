/**
 * Phase 20 — the itinerary while it is being written.
 *
 * The builder's reply is JSON and reaches the page a few tokens at a time
 * (`builder_token` events). `readPartialJson` reads as much of it as has
 * arrived — an object that is not closed yet, a string that stops mid-word —
 * and `readDraft` turns that into what the page shows: the days so far, each
 * with the places named so far.
 *
 * Pure functions, no React. What they read is the model's unchecked text: a
 * preview. The itinerary the page shows afterwards is the one the backend
 * checked and saved.
 */

import { FREE_TIME, SLOTS, SLOT_LABELS } from "./map";

// ── Reading JSON that is not finished ───────────────────────────────────────

export interface PartialJson {
  /** Everything that could be read. A string that was cut off is kept as far as it goes. */
  value: unknown;
  /** False when the text stopped before the value was closed. */
  complete: boolean;
  /** The string the text stopped in the middle of, as far as it goes; null when it did not stop inside one. */
  cutString: string | null;
}

const MISSING = Symbol("missing");
const ESCAPES: Record<string, string> = { n: "\n", t: "\t", r: "\r", b: "\b", f: "\f" };
const NUMBER_OR_LITERAL = /^(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)/;
const LITERALS: Record<string, unknown> = { true: true, false: false, null: null };

/** Read the JSON value that starts at the first "{" of `text`, as far as the text goes. Null when there is none yet. */
export function readPartialJson(text: string): PartialJson | null {
  let at = text.indexOf("{"); // a model may open with a ```json fence or a word of explanation
  if (at < 0) return null;
  let complete = true;
  let cutString: string | null = null;

  const end = () => at >= text.length;
  const stop = () => {
    complete = false;
    at = text.length;
  };
  const skipSpace = () => {
    while (!end() && /\s/.test(text[at])) at++;
  };

  function readString(): string {
    let out = "";
    at++; // the opening quote
    while (!end()) {
      const ch = text[at];
      if (ch === '"') {
        at++;
        return out;
      }
      if (ch !== "\\") {
        out += ch;
        at++;
        continue;
      }
      const escaped = text[at + 1];
      if (escaped === undefined) break; // the escape itself is cut off
      if (escaped === "u") {
        const hex = text.slice(at + 2, at + 6);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) break;
        out += String.fromCharCode(parseInt(hex, 16));
        at += 6;
      } else {
        out += ESCAPES[escaped] ?? escaped;
        at += 2;
      }
    }
    stop(); // cut off mid-string: what there is of it
    cutString = out;
    return out;
  }

  function readArray(): unknown[] {
    const items: unknown[] = [];
    at++;
    for (;;) {
      skipSpace();
      if (end()) {
        stop();
        return items;
      }
      if (text[at] === "]") {
        at++;
        return items;
      }
      if (text[at] === ",") {
        at++;
        continue;
      }
      const item = readValue();
      if (item === MISSING) return items;
      items.push(item);
    }
  }

  function readObject(): Record<string, unknown> {
    const object: Record<string, unknown> = {};
    at++;
    for (;;) {
      skipSpace();
      if (end()) {
        stop();
        return object;
      }
      if (text[at] === "}") {
        at++;
        return object;
      }
      if (text[at] === ",") {
        at++;
        continue;
      }
      if (text[at] !== '"') {
        stop(); // not JSON this can read
        return object;
      }
      const key = readString();
      if (!complete) return object; // the key itself was cut off: it names nothing yet
      skipSpace();
      if (text[at] !== ":") {
        stop();
        return object;
      }
      at++;
      const value = readValue();
      if (value !== MISSING) object[key] = value;
      if (!complete) return object;
    }
  }

  function readValue(): unknown {
    skipSpace();
    if (end()) {
      stop();
      return MISSING;
    }
    const ch = text[at];
    if (ch === '"') return readString();
    if (ch === "{") return readObject();
    if (ch === "[") return readArray();

    const match = NUMBER_OR_LITERAL.exec(text.slice(at, at + 32)); // nothing here is longer than that
    // A number that runs up to the end of the text may have more digits coming: 172 of 17200.
    if (!match || (at + match[0].length === text.length && !(match[0] in LITERALS))) {
      stop();
      return MISSING;
    }
    at += match[0].length;
    return match[0] in LITERALS ? LITERALS[match[0]] : Number(match[0]);
  }

  const value = readValue();
  return value === MISSING ? null : { value, complete, cutString };
}

// ── What the page shows of it ───────────────────────────────────────────────

export interface DraftLine {
  /** "Morning", "Afternoon", "Evening", "All day" or "Stay" */
  label: string;
  /** The place, as far as it has been written. */
  text: string;
}

export interface DraftDay {
  day: number;
  /** "YYYY-MM-DD" once the whole date has arrived. */
  date: string | null;
  lines: DraftLine[];
}

export interface Draft {
  days: DraftDay[];
  /** The whole reply has arrived. It is still being checked: only the saved itinerary is final. */
  complete: boolean;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * The lines of one day. `pending` is a place name that is still arriving and cannot be shown yet
 * (see readDraft); the slot it belongs to is left out until it can.
 */
function linesOf(day: Record<string, unknown>, pending: string | null): DraftLine[] {
  const stops: DraftLine[] = [];
  for (const slot of SLOTS) {
    const entry = day[slot];
    if (isRecord(entry) && typeof entry.activity === "string" && entry.activity !== pending) {
      stops.push({ label: SLOT_LABELS[slot], text: entry.activity });
    }
  }
  // Free time is said once, and not beside a real stop — the rule the day cards follow (lib/map.ts).
  const real = stops.filter((line) => line.text !== FREE_TIME);
  const lines = real.length > 0 ? real : stops.length > 0 ? [{ label: "All day", text: "Free time" }] : [];

  if (isRecord(day.hotel) && typeof day.hotel.name === "string") lines.push({ label: "Stay", text: day.hotel.name });
  return lines;
}

/** The days written so far, read out of the reply as far as it has arrived. */
export function readDraft(text: string): Draft {
  const parsed = readPartialJson(text);
  const root = parsed && isRecord(parsed.value) ? parsed.value : {};
  const written = Array.isArray(root.days) ? root.days.filter(isRecord) : [];

  // "Explore the area" is how the builder says free time. While those words are still arriving
  // they would read as the start of a place name ("Explore the ar"), so a name that could still
  // become them waits until it is clear which it is.
  const cut = parsed?.cutString ?? null;
  const pending = cut !== null && cut !== FREE_TIME && FREE_TIME.startsWith(cut) ? cut : null;

  const days = written.map((day, index) => ({
    day: typeof day.day === "number" ? day.day : index + 1,
    date: typeof day.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(day.date) ? day.date : null,
    lines: linesOf(day, index === written.length - 1 ? pending : null),
  }));

  return { days, complete: parsed?.complete ?? false };
}

/** One line for where the writing has got to: "Day 2 · Afternoon — Baga Beach". Null before the first day. */
export function draftProgress(draft: Draft): string | null {
  const day = draft.days[draft.days.length - 1];
  if (!day) return null;
  const line = day.lines[day.lines.length - 1];
  return line && line.text ? `Day ${day.day} · ${line.label} — ${line.text}` : `Day ${day.day}`;
}
