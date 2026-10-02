"use client";

import dynamic from "next/dynamic";
import { ArrowDown } from "lucide-react";
import { useRef, useState } from "react";

import { nightsBetween } from "@/lib/format";
import type { Itinerary, Trip } from "@/lib/types";
import CostSummary from "./CostSummary";
import DayCard from "./DayCard";
import type { MapFocus } from "./ItineraryMap";

// Leaflet touches `window` at import time, so the map only ever loads in the browser.
const ItineraryMap = dynamic(() => import("./ItineraryMap"), {
  ssr: false,
  loading: () => <div className="skeleton h-[520px]" aria-hidden />,
});

interface Props {
  itinerary: Itinerary;
  /** For the budget the total is measured against. */
  trip: Trip | null;
  /** A refinement is running: the current plan stays on screen, dimmed, until the new one replaces it. */
  updating?: boolean;
}

export default function ItineraryView({ itinerary, trip, updating = false }: Props) {
  const data = itinerary.structured_data;
  const mapRef = useRef<HTMLDivElement>(null);
  const [focus, setFocus] = useState<MapFocus | null>(null);

  if (!data) {
    return (
      <div className="rounded-2xl border border-dashed border-ink-200 p-8 text-center text-sm text-ink-600">
        Itinerary data not yet available.
      </div>
    );
  }

  const stay = data.days.reduce((sum, d) => sum + (d.hotel?.cost_per_night ?? 0), 0);
  const activities = data.days.reduce(
    (sum, d) => sum + (d.morning?.cost ?? 0) + (d.afternoon?.cost ?? 0) + (d.evening?.cost ?? 0),
    0,
  );
  // Itineraries saved before the flight was attached only carry it inside the total.
  const flights = data.days[0]?.flight?.price_inr ?? Math.max(0, data.total_cost - stay - activities);

  function showOnMap(pinId: string) {
    setFocus({ id: pinId, nonce: Date.now() });
    mapRef.current?.scrollIntoView({ block: "center" }); // smooth unless the user asked for less motion (globals.css)
  }

  return (
    <section
      aria-label="Your itinerary"
      aria-busy={updating}
      className={`space-y-6 transition-opacity duration-300 ${updating ? "opacity-60" : ""}`}
    >
      <CostSummary
        total={data.total_cost}
        budget={trip?.budget ?? null}
        flights={flights}
        stay={stay}
        activities={activities}
        nights={data.days.filter((d) => d.hotel).length || (trip ? nightsBetween(trip.start_date, trip.end_date) : 0)}
        travellers={trip?.group_size ?? null}
      />

      <div className="flex items-end justify-between gap-4 pt-2">
        <div>
          <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">Day by day</h2>
          <p className="mt-0.5 text-sm text-ink-600">Each day has its own colour — the same one on the map.</p>
        </div>
        <a href="#trip-map" className="btn-ghost shrink-0 px-3 py-2">
          <ArrowDown className="h-4 w-4" aria-hidden />
          Map
        </a>
      </div>

      <ol className="space-y-4">
        {data.days.map((day) => (
          <li key={day.day}>
            <DayCard day={day} onShowOnMap={showOnMap} />
          </li>
        ))}
      </ol>

      {/* Map (Phase 18) */}
      <div ref={mapRef} id="trip-map">
        <ItineraryMap days={data.days} focus={focus} />
      </div>
    </section>
  );
}
