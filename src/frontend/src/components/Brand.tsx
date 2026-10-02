/** The logo: a paper plane in the sunset gradient on an ink tile. */

import { useId } from "react";

export function LogoMark({ size = 32, tone = "ink" }: { size?: number; tone?: "ink" | "paper" }) {
  // One gradient per mark. With a shared id every mark paints from the first one in the document,
  // and goes blank when that one is inside a hidden element (the sign-in artwork on a small screen).
  const gradient = useId();
  return (
    <span
      className={`grid shrink-0 place-items-center rounded-[28%] shadow-sm ${tone === "paper" ? "bg-white/10 ring-1 ring-white/10" : "bg-ink-900"}`}
      style={{ width: size, height: size }}
      aria-hidden
    >
      <svg viewBox="0 0 24 24" width={size * 0.58} height={size * 0.58} fill="none">
        <defs>
          <linearGradient id={gradient} x1="3" y1="21" x2="21" y2="3" gradientUnits="userSpaceOnUse">
            <stop stopColor="#f6b73c" />
            <stop offset="0.55" stopColor="#f2703f" />
            <stop offset="1" stopColor="#e0457b" />
          </linearGradient>
        </defs>
        <path d="M21 3 3 10.2l6.9 2.9L12.8 20 21 3Z" fill={`url(#${gradient})`} stroke={`url(#${gradient})`} strokeWidth="1.6" strokeLinejoin="round" />
        <path d="M9.9 13.1 21 3" stroke="#0b0b0b" strokeOpacity="0.35" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export default function Brand({ size = 32, tone = "ink" }: { size?: number; tone?: "ink" | "paper" }) {
  return (
    <span className="flex items-center gap-2.5">
      <LogoMark size={size} tone={tone} />
      <span className={`font-display text-[19px] font-semibold tracking-tight ${tone === "paper" ? "text-white" : "text-ink-900"}`}>
        TripPlanner
      </span>
    </span>
  );
}
