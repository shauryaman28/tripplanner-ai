"use client";

import { Bus, ChevronDown, Clock, Handshake, ShieldCheck, TriangleAlert, type LucideIcon } from "lucide-react";
import { useId, useState } from "react";

import { tipSections, type TipSection, type TipSectionKey } from "@/lib/tips";
import type { DaySchedule } from "@/lib/types";

interface Props {
  /** `structured_data.local_intelligence` — whatever is there; an itinerary without tips shows no section. */
  intelligence: unknown;
  /** The plan's days: a place the plan visits is marked, and listed first. */
  days: DaySchedule[];
  destination: string | null;
}

const ICONS: Record<TipSectionKey, LucideIcon> = {
  local_transport: Bus,
  cultural_norms:  Handshake,
  tourist_traps:   TriangleAlert,
  best_times:      Clock,
  safety_tips:     ShieldCheck,
};

/** One section: a heading that is a button, and the panel it opens (the WAI-ARIA accordion pattern). */
function Section({ section, open, onToggle }: { section: TipSection; open: boolean; onToggle: () => void }) {
  const id = useId();
  const Icon = ICONS[section.key];
  const count = section.tips.length;
  return (
    <div data-tips-section={section.key}>
      <h3>
        <button
          id={`${id}-heading`}
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={`${id}-panel`}
          className="focus-ring group flex w-full items-center gap-3 rounded-xl px-4 py-3.5 text-left sm:px-5"
        >
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-ink-100 text-ink-700 transition-colors group-hover:bg-ink-900 group-hover:text-white">
            <Icon className="h-4 w-4" aria-hidden />
          </span>
          <span className="min-w-0 flex-1 text-[15px] font-medium text-ink-900">{section.label}</span>
          {/* how much is inside, so a closed section is not a blind click */}
          {count > 1 && (
            <span className="shrink-0 text-xs tabular-nums text-ink-500">
              {count} {section.key === "best_times" ? "places" : "tips"}
            </span>
          )}
          <ChevronDown className={`h-4 w-4 shrink-0 text-ink-400 transition-transform duration-200 ${open ? "rotate-180" : ""}`} aria-hidden />
        </button>
      </h3>

      <div id={`${id}-panel`} role="region" aria-labelledby={`${id}-heading`} hidden={!open} className="px-4 pb-4 pl-[60px] sm:px-5 sm:pl-[64px]">
        {section.key === "best_times" ? (
          <ul className="space-y-3">
            {section.tips.map((tip) => (
              <li key={tip.place} className="text-sm leading-relaxed text-ink-700">
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium text-ink-900">{tip.place}</span>
                  {tip.inPlan && (
                    <span className="rounded-full bg-good-soft px-2 py-0.5 text-[11px] font-medium text-good-ink">In your plan · {tip.inPlan}</span>
                  )}
                </p>
                <p className="break-words">{tip.text}</p>
              </li>
            ))}
          </ul>
        ) : count === 1 ? (
          <p className="break-words text-sm leading-relaxed text-ink-700">{section.tips[0].text}</p>
        ) : (
          <ul className="list-disc space-y-1.5 pl-4 text-sm leading-relaxed text-ink-700 marker:text-ink-300">
            {section.tips.map((tip) => (
              <li key={tip.text} className="break-words">
                {tip.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * Phase 22 — "Local tips": what a guide would tell you about the place, under the day-by-day plan.
 *
 * An accordion, every section closed until it is asked for: the plan is the
 * page's subject, and this is reference to dip into. It renders nothing at all
 * when the itinerary has no tips — the agent behind them is the one part of a
 * plan allowed to be missing, and a plan without them is not an error.
 */
export default function LocalTips({ intelligence, days, destination }: Props) {
  const sections = tipSections(intelligence, days);
  const [open, setOpen] = useState<ReadonlySet<TipSectionKey>>(new Set());
  if (sections.length === 0) return null;

  const toggle = (key: TipSectionKey) =>
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  return (
    <section aria-label="Local tips" className="pt-2">
      <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">Local tips</h2>
      {/* where this comes from, said before it is read: nothing here was looked up */}
      <p className="mt-0.5 max-w-prose text-sm text-ink-600">
        General advice from the assistant&apos;s own knowledge of {destination || "the place"}, not from a live source. Prices and timings
        change — check locally.
      </p>
      <div className="card mt-4 divide-y divide-ink-100">
        {sections.map((section) => (
          <Section key={section.key} section={section} open={open.has(section.key)} onToggle={() => toggle(section.key)} />
        ))}
      </div>
    </section>
  );
}
