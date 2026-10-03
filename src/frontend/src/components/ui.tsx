/** Small shared pieces: spinner, status badge, the map pin as a React element, the marks of a change. */

import { CircleAlert, CircleCheck, CircleDashed, Loader2 } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import type { PinStyle } from "@/lib/map";
import type { TripStatus } from "@/lib/types";

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return <Loader2 className={`animate-spin ${className}`} aria-hidden />;
}

/** The same mark the map draws (see `.pin` in globals.css), for legends and day cards. */
export function Pin({
  pinStyle,
  size,
  children,
}: {
  pinStyle: PinStyle;
  size?: "xs" | "sm";
  children?: ReactNode;
}) {
  return (
    <span
      className="pin"
      data-shape={pinStyle.shape}
      data-size={size}
      style={{ "--pin": pinStyle.color, "--pin-ink": pinStyle.ink } as CSSProperties}
      aria-hidden
    >
      <span>{children}</span>
    </span>
  );
}

// Status is never colour alone: every state has its own icon and words.
const STATUS: Record<TripStatus, { label: string; className: string; icon: ReactNode }> = {
  completed: { label: "Planned",         className: "bg-good-soft text-good-ink", icon: <CircleCheck className="h-3.5 w-3.5" aria-hidden /> },
  planning:  { label: "Planning…",       className: "bg-ink-100 text-ink-700",    icon: <Spinner className="h-3.5 w-3.5" /> },
  failed:    { label: "Needs attention", className: "bg-bad-soft text-bad-ink",   icon: <CircleAlert className="h-3.5 w-3.5" aria-hidden /> },
  pending:   { label: "Not planned yet", className: "bg-ink-100 text-ink-600",    icon: <CircleDashed className="h-3.5 w-3.5" aria-hidden /> },
};

export function TripStatusBadge({ status, label }: { status: TripStatus; label?: string }) {
  const meta = STATUS[status] ?? STATUS.pending;
  return (
    <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${meta.className}`}>
      {meta.icon}
      {label ?? meta.label}
    </span>
  );
}

// ── A change request, on the plan (Phase 20) ───────────────────────────────
// Only the part being changed moves; afterwards the parts that came back different are marked for
// a few seconds. Neither is colour alone: both come with words.

/** Beside a part of the plan a run is working on. */
export function UpdatingTag() {
  return (
    <span role="status" className="inline-flex shrink-0 items-center gap-1.5 self-center text-xs font-medium text-ink-500">
      <Spinner className="h-3.5 w-3.5" />
      Updating…
    </span>
  );
}

/** On a part of the plan that has just changed. */
export function UpdatedTag() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-good-soft px-1.5 py-0.5 text-[10px] font-semibold normal-case tracking-normal text-good-ink">
      <CircleCheck className="h-3 w-3" aria-hidden />
      Updated
    </span>
  );
}

/** The classes of a part of the plan, by what is happening to it (globals.css). */
export function partClass(updating: boolean, changed: boolean): string {
  return `${updating ? "is-updating" : ""} ${changed ? "flash-changed" : ""}`;
}
