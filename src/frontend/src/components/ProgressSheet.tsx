"use client";

import { ChevronUp, CircleAlert } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Spinner } from "./ui";

interface Props {
  /** One line for the closed sheet: "Searching — 1 of 3 done", "The flight search failed". */
  title: string;
  /** "busy" while a run is in flight; "alert" when something needs the traveller's attention. */
  tone: "busy" | "alert";
  /** Opened to the full panel. The page holds this: nothing else may sit over an open sheet. */
  open: boolean;
  onToggle: () => void;
  /** The planning progress — shown when the sheet is opened. */
  children: ReactNode;
}

/**
 * Phase 20 — the planning progress on a small screen.
 *
 * There the assistant sits below the whole plan, so its progress panel is out
 * of sight exactly while it matters. On a phone the panel lives in this sheet
 * instead: a bar pinned to the bottom of the screen that says in one line what
 * is happening, and opens to the full panel. It is rendered only below the
 * `lg` breakpoint (the page decides), so the panel is in the document once.
 * The page pins it to the foot of the screen.
 */
export default function ProgressSheet({ title, tone, open, onToggle, children }: Props) {
  const panel = useId();

  return (
    <div data-progress-sheet className="pointer-events-auto w-full">
      <div className="card animate-rise overflow-hidden shadow-float">
        <button
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={panel}
          className="focus-ring flex w-full items-center gap-3 px-4 py-3 text-left"
        >
          {tone === "busy" ? (
            <Spinner className="h-4 w-4 shrink-0 text-ink-500" />
          ) : (
            <CircleAlert className="h-4 w-4 shrink-0 text-bad" aria-hidden />
          )}
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-ink-900">{title}</span>
            <span className="block text-[11px] text-ink-500">{open ? "Tap to close" : "Tap for details"}</span>
          </span>
          <ChevronUp className={`h-4 w-4 shrink-0 text-ink-400 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
        </button>

        <div id={panel} hidden={!open} className="max-h-[55dvh] overflow-y-auto border-t border-ink-200/70 px-4 py-3.5">
          {children}
        </div>
      </div>
    </div>
  );
}
