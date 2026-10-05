"use client";

import dynamic from "next/dynamic";
import { ArrowDown } from "lucide-react";
import { useRef, useState } from "react";

import type { PlanSection } from "@/lib/changes";
import { nightsBetween } from "@/lib/format";
import type { Itinerary, Trip } from "@/lib/types";
import CostSummary from "./CostSummary";
import DayCard from "./DayCard";
import DownloadPdfButton from "./DownloadPdfButton";
import type { MapFocus } from "./ItineraryMap";
import LocalTips from "./LocalTips";
import SimilarTrips from "./SimilarTrips";

// Leaflet touches `window` at import time, so the map only ever loads in the browser.
const ItineraryMap = dynamic(() => import("./ItineraryMap"), {
  ssr: false,
  loading: () => <div className="skeleton h-[520px]" aria-hidden />,
});

/**
 * What a run in flight is doing to the plan on screen (Phase 20). A targeted change works on one
 * section — only that one shows it, the rest stays as it is. "all": the whole trip is being
 * planned again. "pending": a change was asked for and it is not known yet what it touches.
 */
export type Updating = PlanSection | "all" | "pending" | null;

interface Props {
  itinerary: Itinerary;
  /** For the budget the total is measured against. */
  trip: Trip | null;
  /** The current plan stays on screen while a change is made; this says which part of it is being changed. */
  updating?: Updating;
  /** The parts that came back different from the last change — marked "Updated" for a few seconds. */
  changed?: ReadonlySet<string>;
}

const NOTHING_CHANGED: ReadonlySet<string> = new Set();

export default function ItineraryView({ itinerary, trip, updating = null, changed = NOTHING_CHANGED }: Props) {
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

  // one section of the plan, or none: "all" dims the whole plan instead, "pending" touches nothing yet
  const section = updating === "all" || updating === "pending" ? null : updating;

  return (
    <section
      aria-label="Your itinerary"
      aria-busy={updating !== null}
      data-updating={updating ?? undefined}
      className={`space-y-6 transition-opacity duration-300 ${updating === "all" ? "opacity-60" : ""}`}
    >
      <CostSummary
        total={data.total_cost}
        budget={trip?.budget ?? null}
        flights={flights}
        stay={stay}
        activities={activities}
        nights={data.days.filter((d) => d.hotel).length || (trip ? nightsBetween(trip.start_date, trip.end_date) : 0)}
        travellers={trip?.group_size ?? null}
        updating={section}
        changed={changed}
      />

      {/* on a narrow screen the two actions drop under the heading instead of squeezing it */}
      <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-3 pt-2">
        <div>
          <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">Day by day</h2>
          <p className="mt-0.5 text-sm text-ink-600">Each day has its own colour — the same one on the map.</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* PDF export (Phase 19) */}
          <DownloadPdfButton tripId={itinerary.trip_id} disabled={updating !== null} />
          <a href="#trip-map" className="btn-ghost shrink-0 px-3 py-2">
            <ArrowDown className="h-4 w-4" aria-hidden />
            Map
          </a>
        </div>
      </div>

      <ol className="space-y-4">
        {data.days.map((day) => (
          <li key={day.day}>
            <DayCard day={day} onShowOnMap={showOnMap} updating={section} changed={changed} />
          </li>
        ))}
      </ol>

      {/* Local tips (Phase 22) — nothing at all when the itinerary has none. */}
      <LocalTips intelligence={data.local_intelligence} days={data.days} destination={trip?.destination ?? null} />

      {/* Map (Phase 18). It draws the stops, so it waits with them. */}
      <div ref={mapRef} id="trip-map" className={`transition-opacity duration-300 ${section === "activities" ? "opacity-60" : ""}`}>
        <ItineraryMap days={data.days} focus={focus} />
      </div>

      {/* Similar trips (Phase 23) — nothing at all when the traveller has none like this one. */}
      <SimilarTrips tripId={itinerary.trip_id} itineraryId={itinerary.id} />
    </section>
  );
}
