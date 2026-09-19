"use client";

import type { DaySchedule } from "@/lib/types";

interface Props {
  day: DaySchedule;
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

function formatINR(amount: number) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amount);
}

const SLOT_LABELS: Record<string, string> = {
  morning:   "Morning",
  afternoon: "Afternoon",
  evening:   "Evening",
};

const SLOT_ICONS: Record<string, string> = {
  morning:   "🌅",
  afternoon: "☀️",
  evening:   "🌆",
};

interface SlotRow {
  key: string;
  slot: DaySchedule["morning"];
}

export default function DayCard({ day }: Props) {
  const slots: SlotRow[] = [
    { key: "morning",   slot: day.morning },
    { key: "afternoon", slot: day.afternoon },
    { key: "evening",   slot: day.evening },
  ];

  const dayCost = slots.reduce((sum, { slot }) => sum + (slot?.cost ?? 0), 0)
    + (day.hotel?.cost_per_night ?? 0);

  return (
    <article className="card overflow-hidden animate-slide-up">
      {/* Day header */}
      <header className="flex items-center justify-between bg-brand-50 px-4 py-3">
        <div>
          <span className="text-xs font-medium text-brand-600 uppercase tracking-wide">
            Day {day.day}
          </span>
          <p className="font-display text-sm font-semibold text-gray-900">{formatDate(day.date)}</p>
        </div>
        {dayCost > 0 && (
          <span className="text-sm font-medium text-gray-600">{formatINR(dayCost)}</span>
        )}
      </header>

      <div className="divide-y divide-gray-50 px-4">
        {/* Activity slots */}
        {slots.map(({ key, slot }) => {
          if (!slot) return null;
          return (
            <div key={key} className="flex items-start gap-3 py-3">
              <span className="mt-0.5 text-base" aria-hidden>{SLOT_ICONS[key]}</span>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-muted">{SLOT_LABELS[key]}</p>
                <p className="text-sm font-medium text-gray-900">{slot.activity}</p>
                {slot.cost > 0 && (
                  <p className="text-xs text-muted">{formatINR(slot.cost)}</p>
                )}
              </div>
            </div>
          );
        })}

        {/* Hotel */}
        {day.hotel && (
          <div className="flex items-start gap-3 py-3">
            <span className="mt-0.5 text-base" aria-hidden>🏨</span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted">Hotel</p>
              <p className="text-sm font-medium text-gray-900">{day.hotel.name}</p>
              <p className="text-xs text-muted">{formatINR(day.hotel.cost_per_night)} / night</p>
            </div>
          </div>
        )}

        {/* Flight if present */}
        {day.flight && (
          <div className="flex items-start gap-3 py-3">
            <span className="mt-0.5 text-base" aria-hidden>✈️</span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-medium text-muted">Flight</p>
              <p className="text-sm font-medium text-gray-900">
                {(day.flight as { airline?: string; flight_number?: string }).airline ?? ""}
                {" "}
                {(day.flight as { flight_number?: string }).flight_number ?? ""}
              </p>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}
