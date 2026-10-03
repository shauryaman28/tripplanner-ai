"use client";

import { useSyncExternalStore } from "react";

// Tailwind's `lg` breakpoint: from here up the assistant sits beside the plan instead of below it.
const WIDE = "(min-width: 1024px)";

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(WIDE);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/** The two-column layout is in use. For a single call (an effect, a handler) rather than rendering. */
export function isWideScreen(): boolean {
  return window.matchMedia(WIDE).matches;
}

/**
 * Whether the two-column layout is in use — for the few things that have to be rendered in one
 * place or the other rather than hidden with CSS (the planning progress: beside the plan on a wide
 * screen, in a bottom sheet on a small one). The server, which cannot know, renders the wide layout.
 */
export function useWideScreen(): boolean {
  return useSyncExternalStore(subscribe, isWideScreen, () => true);
}
