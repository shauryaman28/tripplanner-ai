"use client";

import Link from "next/link";
import { ArrowRight, CalendarDays, MapPin, Wallet } from "lucide-react";
import { useEffect, useState } from "react";

import { getSimilarTrips } from "@/lib/api";
import { formatDateRange, formatINR } from "@/lib/format";
import type { TripMatch } from "@/lib/types";

interface Props {
  tripId: string;
  /** The itinerary on screen: when a change saves a new one, what it is like may have changed too. */
  itineraryId: string;
}

/** How many of the (up to five) similar trips the page shows. */
const SHOWN = 3;
// A trip's embedding is made in the background just after its plan is saved. Asked before it is
// there, the backend answers "pending": ask again a few times, a little later each time.
const RETRY_AFTER_MS = [1500, 3000, 6000];

/**
 * Phase 23 — "Similar trips": the traveller's own earlier trips most like this one.
 *
 * Each is a small card — where, when, what it came to, and one place to remember it by — that
 * opens that trip. Like the local tips it is an extra: when there is nothing alike, or the
 * question cannot be answered, there is simply no section.
 */
export default function SimilarTrips({ tripId, itineraryId }: Props) {
  const [matches, setMatches] = useState<TripMatch[]>([]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function load(attempt: number) {
      try {
        const answer = await getSimilarTrips(tripId);
        if (cancelled) return;
        if (answer.status === "pending" && attempt < RETRY_AFTER_MS.length) {
          timer = setTimeout(() => load(attempt + 1), RETRY_AFTER_MS[attempt]);
          return;
        }
        setMatches(answer.results.slice(0, SHOWN));
      } catch {
        if (!cancelled) setMatches([]); // an extra: failing to load it is not worth a message
      }
    }

    load(0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [tripId, itineraryId]);

  if (matches.length === 0) return null;

  return (
    <section aria-label="Similar trips" className="pt-2">
      <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">Similar trips</h2>
      <p className="mt-0.5 text-sm text-ink-600">Other trips of yours most like this one.</p>
      <ul className="mt-4 grid gap-4 sm:grid-cols-3">
        {matches.map(({ trip, total_cost, highlight, similarity }) => (
          <li key={trip.id}>
            <Link
              href={`/trips/${trip.id}`}
              data-similarity={similarity}
              className="card focus-ring group flex h-full flex-col p-4 transition duration-200 hover:-translate-y-0.5 hover:shadow-lift"
            >
              <h3 className="line-clamp-2 break-words font-display text-lg font-medium leading-snug text-ink-900">{trip.destination}</h3>
              <ul className="mt-2.5 space-y-1.5 text-sm text-ink-600">
                <li className="flex items-center gap-2">
                  <CalendarDays className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
                  {formatDateRange(trip.start_date, trip.end_date)}
                </li>
                {total_cost !== null && (
                  <li className="flex items-center gap-2">
                    <Wallet className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
                    {formatINR(total_cost)} in all
                  </li>
                )}
                {highlight && (
                  <li className="flex items-start gap-2">
                    <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-ink-400" aria-hidden />
                    <span className="min-w-0 break-words">{highlight}</span>
                  </li>
                )}
              </ul>
              <p className="mt-auto flex items-center gap-1 pt-4 text-sm font-medium text-ink-900">
                View itinerary
                <ArrowRight className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5" aria-hidden />
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
