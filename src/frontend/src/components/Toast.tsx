"use client";

import { CircleAlert, Info, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

export interface ToastMessage {
  /** changes with every message, so the same words shown twice still restart the timer */
  id: number;
  tone: "error" | "info";
  text: string;
}

const LIFETIME_MS = 7_000;

/** One toast at a time: a new message replaces the one on screen, and each leaves by itself. */
export function useToast() {
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const show = useCallback((tone: ToastMessage["tone"], text: string) => setToast({ id: Date.now(), tone, text }), []);
  const dismiss = useCallback(() => setToast(null), []);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(dismiss, LIFETIME_MS);
    return () => clearTimeout(timer);
  }, [toast, dismiss]);

  return { toast, show, dismiss };
}

// The tone is an icon plus the words — never the colour alone.
const TONES = {
  error: { icon: CircleAlert, tile: "bg-bad-soft text-bad-ink", role: "alert" },
  info:  { icon: Info,        tile: "bg-ink-100 text-ink-700",  role: "status" },
} as const;

/**
 * A short message over the page, for something that happened away from where the user is looking.
 *
 * It is rendered into <body>: the plan it reports on is dimmed while it updates, and a toast inside
 * it would dim too. It sits under the header rather than at the bottom, where a phone has the
 * "Ask for a change" button.
 */
export default function Toast({ toast, onDismiss }: { toast: ToastMessage | null; onDismiss: () => void }) {
  if (!toast) return null;
  const { icon: Icon, tile, role } = TONES[toast.tone];

  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-20 z-50 flex justify-center px-4" data-toast={toast.tone}>
      <div
        key={toast.id}
        role={role}
        className="pointer-events-auto flex max-w-md animate-rise items-start gap-3 rounded-2xl border border-ink-200/70 bg-white py-3 pl-3 pr-2 shadow-float"
      >
        <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-xl ${tile}`}>
          <Icon className="h-[18px] w-[18px]" aria-hidden />
        </span>
        <p className="min-w-0 self-center text-sm leading-snug text-ink-900">{toast.text}</p>
        <button
          onClick={onDismiss}
          aria-label="Dismiss"
          className="focus-ring grid h-8 w-8 shrink-0 place-items-center rounded-full text-ink-400 transition-colors hover:bg-ink-100 hover:text-ink-900"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>
    </div>,
    document.body,
  );
}
