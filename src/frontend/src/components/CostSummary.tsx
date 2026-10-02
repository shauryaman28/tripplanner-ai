"use client";

import { BedDouble, CircleAlert, CircleCheck, Plane, Ticket, TriangleAlert, type LucideIcon } from "lucide-react";

import { formatINR, plural } from "@/lib/format";

interface Props {
  total: number;
  /** the trip's budget; null when it is not known */
  budget: number | null;
  flights: number;
  stay: number;
  activities: number;
  nights: number;
  travellers: number | null;
}

type BudgetState = "under" | "tight" | "over";

// The fill carries the state; the track is a lighter step of the same colour.
const METER: Record<BudgetState, { fill: string; track: string }> = {
  under: { fill: "bg-ink-900", track: "bg-ink-200" },
  tight: { fill: "bg-warn",    track: "bg-[#fbe7b0]" },
  over:  { fill: "bg-bad",     track: "bg-[#f6cfcf]" },
};

function BudgetNote({ state, total, budget }: { state: BudgetState; total: number; budget: number }) {
  const gap = Math.abs(budget - total);
  const tone = { under: "bg-good-soft text-good-ink", tight: "bg-warn-soft text-warn-ink", over: "bg-bad-soft text-bad-ink" }[state];
  const Icon = { under: CircleCheck, tight: TriangleAlert, over: CircleAlert }[state];
  const text = {
    under: `${formatINR(gap)} under budget`,
    tight: `Only ${formatINR(gap)} of the budget left`,
    over:  `${formatINR(gap)} over budget`,
  }[state];

  return (
    <p className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${tone}`}>
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {text}
    </p>
  );
}

function Stat({ icon: Icon, label, value, note }: { icon: LucideIcon; label: string; value: number; note: string }) {
  return (
    <div className="bg-white px-4 py-3.5">
      <dt className="flex items-center gap-1.5 text-xs font-medium text-ink-500">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </dt>
      <dd className="mt-1 text-lg font-semibold text-ink-900">{formatINR(value)}</dd>
      <dd className="text-xs text-ink-500">{note}</dd>
    </div>
  );
}

/** The trip's one headline number, how much of the budget it uses, and what it is made of. */
export default function CostSummary({ total, budget, flights, stay, activities, nights, travellers }: Props) {
  const used = budget ? total / budget : null;
  const state: BudgetState = used === null || used < 0.9 ? "under" : used <= 1 ? "tight" : "over";
  const percent = used === null ? null : Math.round(used * 100);

  return (
    <section aria-label="Trip cost" className="card p-5 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <p className="eyebrow">Estimated total</p>
          <p className="mt-1 text-5xl font-semibold tracking-tight text-ink-900">{formatINR(total)}</p>
        </div>
        {budget !== null && <BudgetNote state={state} total={total} budget={budget} />}
      </div>

      {budget !== null && percent !== null && (
        <div className="mt-5">
          <div
            role="meter"
            aria-label="Budget used"
            aria-valuemin={0}
            aria-valuemax={budget}
            aria-valuenow={Math.min(total, budget)}
            aria-valuetext={`${formatINR(total)} of ${formatINR(budget)}`}
            title={`${formatINR(total)} of ${formatINR(budget)} (${percent}%)`}
            className={`h-2.5 overflow-hidden rounded ${METER[state].track}`}
          >
            <div
              className={`h-full rounded-r transition-[width] duration-500 ${METER[state].fill}`}
              style={{ width: `${Math.min(used ?? 0, 1) * 100}%` }}
            />
          </div>
          <p className="mt-2 text-xs text-ink-500">
            {percent}% of your {formatINR(budget)} budget
          </p>
        </div>
      )}

      <dl className="mt-6 grid gap-px overflow-hidden rounded-xl border border-ink-200/70 bg-ink-200/70 sm:grid-cols-3">
        <Stat
          icon={Plane}
          label="Flights"
          value={flights}
          note={flights > 0 ? `Return${travellers ? `, ${plural(travellers, "traveller")}` : ""}` : "Not included"}
        />
        <Stat icon={BedDouble} label="Stay" value={stay} note={stay > 0 ? plural(nights, "night") : "Not included"} />
        <Stat icon={Ticket} label="Activities" value={activities} note={activities > 0 ? "Entry fees" : "No entry fees listed"} />
      </dl>
    </section>
  );
}
