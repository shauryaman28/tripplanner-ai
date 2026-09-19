"use client";

import type { Itinerary } from "@/lib/types";
import DayCard from "./DayCard";

interface Props {
  itinerary: Itinerary;
}

function formatINR(amount: number) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amount);
}

export default function ItineraryView({ itinerary }: Props) {
  const data = itinerary.structured_data;
  if (!data) {
    return (
      <div className="rounded-xl border border-dashed border-gray-200 p-8 text-center text-sm text-muted">
        Itinerary data not yet available.
      </div>
    );
  }

  const flightCost = data.days.reduce((sum, d) => {
    const f = d.flight as { price_inr?: number } | null;
    return sum + (f?.price_inr ?? 0);
  }, 0);
  const hotelCost = data.days.reduce(
    (sum, d) => sum + (d.hotel?.cost_per_night ?? 0),
    0,
  );
  const activityCost = data.days.reduce((sum, d) => {
    return (
      sum +
      (d.morning?.cost ?? 0) +
      (d.afternoon?.cost ?? 0) +
      (d.evening?.cost ?? 0)
    );
  }, 0);

  return (
    <section aria-label="Your itinerary" className="space-y-5">
      {/* Cost summary pill row */}
      <div className="flex flex-wrap gap-3">
        {[
          { label: "Total",      value: data.total_cost, accent: true },
          { label: "Flights",    value: flightCost },
          { label: "Hotels",     value: hotelCost },
          { label: "Activities", value: activityCost },
        ].map(({ label, value, accent }) => (
          <div
            key={label}
            className={`rounded-xl px-4 py-2.5 ${
              accent
                ? "bg-brand-600 text-white"
                : "bg-white border border-gray-100 text-gray-800"
            }`}
          >
            <p className={`text-xs font-medium ${accent ? "text-brand-100" : "text-muted"}`}>
              {label}
            </p>
            <p className="text-sm font-semibold">{formatINR(value)}</p>
          </div>
        ))}
      </div>

      {/* Day cards */}
      <div className="space-y-4">
        {data.days.map((day) => (
          <DayCard key={day.day} day={day} />
        ))}
      </div>
    </section>
  );
}
