"use client";

import { useMemo } from "react";

import { readDraft } from "@/lib/draft";
import { formatWeekday } from "@/lib/format";
import { dayStyle } from "@/lib/map";
import type { BuilderDraft } from "@/lib/sse";
import { AssistantAvatar } from "./MessageThread";
import { Pin } from "./ui";

/**
 * Phase 20 — the itinerary while it is being written.
 *
 * The builder's reply arrives a few tokens at a time; this shows what has
 * arrived so far, day by day, so the wait is spent reading the plan take
 * shape. It is a preview of unchecked text: the day cards that replace it are
 * the itinerary the backend checked and saved.
 */
export default function LiveDraft({ draft }: { draft: BuilderDraft }) {
  const { days, complete } = useMemo(() => readDraft(draft.text), [draft.text]);
  const lastDay = days.length - 1;

  return (
    // no aria-live: the text changes many times a second. The stage above announces the step once.
    <section aria-label="Itinerary being written" aria-busy={!complete} data-live-draft className="card animate-rise p-5 sm:p-6">
      <div className="flex items-center gap-3">
        <AssistantAvatar size="h-9 w-9" />
        <div className="min-w-0">
          <p className="eyebrow">Draft</p>
          <h3 className="font-display text-xl font-medium tracking-tight text-ink-900">
            {complete ? "Checking the plan…" : "Writing your itinerary…"}
          </h3>
        </div>
      </div>
      <p className="mt-2 text-xs text-ink-500">
        {draft.attempt > 1
          ? "The first draft didn't pass its checks, so it is being written again."
          : "You are reading it as it is written. It is checked and saved once it is complete."}
      </p>

      <ol className="mt-4 space-y-4">
        {days.map((day, index) => (
          <li key={day.day} className="flex gap-3">
            <span className="mt-1">
              <Pin pinStyle={dayStyle(day.day)} size="xs" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-ink-900">
                Day {day.day}
                {day.date && <span className="font-normal text-ink-500"> · {formatWeekday(day.date)}</span>}
              </p>
              <ul className="mt-1 space-y-0.5">
                {day.lines.map((line, at) => (
                  <li key={line.label} className="flex gap-3 text-sm leading-relaxed">
                    <span className="w-[4.75rem] shrink-0 text-xs font-medium leading-relaxed text-ink-500">{line.label}</span>
                    <span className="min-w-0 break-words text-ink-800">
                      {line.text}
                      {!complete && index === lastDay && at === day.lines.length - 1 && <span className="caret" aria-hidden />}
                    </span>
                  </li>
                ))}
              </ul>
              {/* a day that has only just been opened: the caret waits here */}
              {!complete && index === lastDay && day.lines.length === 0 && <span className="caret" aria-hidden />}
            </div>
          </li>
        ))}
      </ol>
      {days.length === 0 && (
        <p className="mt-1 text-sm text-ink-600">
          Starting
          <span className="caret" aria-hidden />
        </p>
      )}
    </section>
  );
}
