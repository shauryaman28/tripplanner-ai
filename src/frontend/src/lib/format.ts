/** Formatting shared by every page: money, dates, durations. */

const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

export function formatINR(amount: number): string {
  return INR.format(Math.round(amount));
}

/**
 * "YYYY-MM-DD" → a local Date. `new Date("YYYY-MM-DD")` is UTC midnight, which
 * is the previous day for anyone west of Greenwich.
 */
export function parseISODate(iso: string): Date {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return new Date(year, (month || 1) - 1, day || 1);
}

/** "Mon, 16 Nov" */
export function formatWeekday(iso: string): string {
  return parseISODate(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
}

/** "16 – 20 Nov 2026", "28 Nov – 2 Dec 2026", "30 Dec 2026 – 3 Jan 2027" */
export function formatDateRange(start: string, end: string): string {
  const from = parseISODate(start);
  const to = parseISODate(end);
  const month = (d: Date) => d.toLocaleDateString("en-IN", { month: "short" });
  const tail = `${to.getDate()} ${month(to)} ${to.getFullYear()}`;

  if (from.getFullYear() !== to.getFullYear()) return `${from.getDate()} ${month(from)} ${from.getFullYear()} – ${tail}`;
  if (from.getMonth() !== to.getMonth()) return `${from.getDate()} ${month(from)} – ${tail}`;
  return `${from.getDate()} – ${tail}`;
}

export function nightsBetween(start: string, end: string): number {
  return Math.max(0, Math.round((parseISODate(end).getTime() - parseISODate(start).getTime()) / 86_400_000));
}

/** "2026-11-16T11:36:00" → "11:36". Flight times are local to the airport, so the string is read as written. */
export function formatClock(isoDateTime?: string | null): string | null {
  return isoDateTime?.match(/T(\d{2}:\d{2})/)?.[1] ?? null;
}

/** 155 → "2h 35m" */
export function formatDuration(minutes?: number | null): string | null {
  if (!minutes || minutes <= 0) return null;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** ["Asha", "Ben", "Dev"] → "Asha, Ben and Dev" (the PDF says it the same way — pdf/formatting.py) */
export function namesInWords(names: string[]): string {
  const said = names.filter(Boolean);
  if (said.length <= 1) return said.join("");
  return `${said.slice(0, -1).join(", ")} and ${said[said.length - 1]}`;
}

/** plural(1, "night") → "1 night"; plural(3, "night") → "3 nights" */
export function plural(count: number, one: string, many: string = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
