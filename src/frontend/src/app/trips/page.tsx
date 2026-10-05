"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, CalendarDays, MapPin, Minus, Plus, Search, Users, Wallet, X } from "lucide-react";
import { useEffect, useState } from "react";

import AppHeader from "@/components/AppHeader";
import { LogoMark } from "@/components/Brand";
import { Spinner, TripStatusBadge } from "@/components/ui";
import { createTrip, getToken, listTrips, searchTrips } from "@/lib/api";
import { formatDateRange, formatINR, nightsBetween, plural } from "@/lib/format";
import type { Trip, TripMatch } from "@/lib/types";

// What the attractions search understands best — one tap adds it to the list.
const INTEREST_IDEAS = ["beach", "history", "food", "nature", "culture", "adventure", "shopping", "nightlife", "wellness"];

// A cover per trip, picked from the destination's name so it never changes between visits.
const COVERS = [
  "from-[#f6b73c] to-[#f2703f]",
  "from-[#56b4e9] to-[#2a78d6]",
  "from-[#1baf7a] to-[#117733]",
  "from-[#f2703f] to-[#e0457b]",
  "from-[#e0457b] to-[#882255]",
  "from-[#4a3aa7] to-[#2a78d6]",
];

function coverFor(destination: string): string {
  const hash = destination.toLowerCase().split("").reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
  return COVERS[hash % COVERS.length];
}

const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local

/** The backend's limit (MAX_TRIP_NIGHTS in schemas/trip.py): longer plans come out mostly empty. */
const MAX_TRIP_NIGHTS = 14;

function dayAfter(iso: string, days: number = 1): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d + days).toLocaleDateString("en-CA");
}

function parseInterests(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

/** A trip as a card. `match` — a search result: the card also says what the plan came to and one place in it. */
function TripCard({ trip, match }: { trip: Trip; match?: TripMatch }) {
  const nights = nightsBetween(trip.start_date, trip.end_date);
  return (
    <Link
      href={`/trips/${trip.id}`}
      data-similarity={match?.similarity}
      className="card focus-ring group flex h-full flex-col overflow-hidden transition duration-200 hover:-translate-y-0.5 hover:shadow-lift"
    >
      <div className={`relative h-24 bg-gradient-to-br ${coverFor(trip.destination)}`}>
        <svg className="absolute inset-0 h-full w-full" viewBox="0 0 400 96" preserveAspectRatio="none" fill="none" aria-hidden>
          <g stroke="#ffffff" strokeOpacity="0.28">
            <path d="M-20 30c80-40 150 30 230-5s130-45 210-5" />
            <path d="M-20 60c80-40 150 30 230-5s130-45 210-5" />
            <path d="M-20 90c80-40 150 30 230-5s130-45 210-5" />
          </g>
        </svg>
        <div className="absolute right-3 top-3 rounded-full bg-white/95 shadow-sm">
          <TripStatusBadge status={trip.status} />
        </div>
      </div>

      <div className="flex flex-1 flex-col p-5">
        <h2 className="line-clamp-2 font-display text-xl font-medium leading-snug text-ink-900">{trip.destination}</h2>

        <ul className="mt-3 space-y-1.5 text-sm text-ink-600">
          <li className="flex items-center gap-2">
            <CalendarDays className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            {formatDateRange(trip.start_date, trip.end_date)} · {plural(nights, "night")}
          </li>
          <li className="flex items-center gap-2">
            <Users className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            {plural(trip.group_size, "traveller")}
          </li>
          <li className="flex items-center gap-2">
            <Wallet className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            {match && match.total_cost !== null
              ? `${formatINR(match.total_cost)} of ${formatINR(trip.budget)} budget`
              : `${formatINR(trip.budget)} budget`}
          </li>
          {match?.highlight && (
            <li className="flex items-start gap-2">
              <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-ink-400" aria-hidden />
              <span className="min-w-0 break-words">{match.highlight}</span>
            </li>
          )}
        </ul>

        {trip.interests && trip.interests.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-1.5">
            {trip.interests.slice(0, 4).map((interest) => (
              <span key={interest} className="chip">
                {interest}
              </span>
            ))}
            {trip.interests.length > 4 && <span className="chip">+{trip.interests.length - 4}</span>}
          </div>
        )}

        <p className="mt-auto flex items-center gap-1 pt-5 text-sm font-medium text-ink-900">
          {trip.status === "completed" ? "View itinerary" : "Open trip"}
          <ArrowRight className="h-4 w-4 transition-transform duration-200 group-hover:translate-x-0.5" aria-hidden />
        </p>
      </div>
    </Link>
  );
}

export default function TripsPage() {
  const router = useRouter();
  const [trips, setTrips] = useState<Trip[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);

  // New trip form
  const [destination, setDestination] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [budget, setBudget] = useState("");
  const [groupSize, setGroupSize] = useState(2);
  const [interests, setInterests] = useState("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Search (Phase 23): what is typed, what was last searched for and found, and whether a search is in flight
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<{ query: string; matches: TripMatch[] } | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    listTrips()
      .then(setTrips)
      .catch(() => router.replace("/login"))
      .finally(() => setLoading(false));
  }, [router]);

  const chosen = parseInterests(interests);
  const nights = startDate && endDate ? nightsBetween(startDate, endDate) : 0;

  function toggleInterest(idea: string) {
    const next = chosen.includes(idea) ? chosen.filter((i) => i !== idea) : [...chosen, idea];
    setInterests(next.join(", "));
  }

  function showAllTrips() {
    setQuery("");
    setFound(null);
    setSearchError(null);
  }

  /** Search the traveller's planned trips by what they are about. An empty box goes back to all of them. */
  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const wanted = query.trim().replace(/\s+/g, " ");
    if (wanted.length < 2) return showAllTrips();
    setSearching(true);
    setSearchError(null);
    try {
      const answer = await searchTrips(wanted);
      setFound({ query: answer.query, matches: answer.results });
    } catch {
      // the list stays as it was: a search that could not run has found nothing out
      setSearchError("Search is not available right now. Try again in a moment.");
    } finally {
      setSearching(false);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (endDate <= startDate) {
      setFormError("The end date must be after the start date.");
      return;
    }
    if (nights > MAX_TRIP_NIGHTS) {
      setFormError(`A trip can be up to ${MAX_TRIP_NIGHTS} nights long — this one is ${nights}.`);
      return;
    }
    setCreating(true);
    try {
      const trip = await createTrip({
        destination: destination.trim(),
        start_date: startDate,
        end_date: endDate,
        budget: parseFloat(budget),
        group_size: groupSize,
        interests: chosen.length ? chosen : undefined,
      });
      router.push(`/trips/${trip.id}`);
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Could not create the trip.");
      setCreating(false);
    }
  }

  return (
    <div className="min-h-dvh">
      <AppHeader />

      <main className="mx-auto max-w-7xl px-4 pb-16 pt-10 sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="font-display text-4xl font-medium tracking-tight text-ink-900 sm:text-5xl">Your trips</h1>
            <p className="mt-2 text-ink-600">
              {loading
                ? "Loading your trips…"
                : trips.length > 0
                ? `${plural(trips.length, "trip")} — pick one up, or start something new.`
                : "Nothing planned yet. Where would you like to go?"}
            </p>
          </div>
          {!showNew && (
            <button onClick={() => setShowNew(true)} className="btn-primary">
              <Plus className="h-4 w-4" aria-hidden />
              Plan a trip
            </button>
          )}
        </div>

        {/* New trip */}
        {showNew && (
          <section aria-label="New trip" className="card mt-8 animate-rise p-5 shadow-lift sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="font-display text-2xl font-medium tracking-tight text-ink-900">New trip</h2>
                <p className="mt-1 text-sm text-ink-600">The basics go here. You describe what you want from it in the chat, next.</p>
              </div>
              <button onClick={() => setShowNew(false)} className="btn-ghost -mr-2 -mt-1 px-2 py-2" aria-label="Close">
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>

            <form onSubmit={handleCreate} className="mt-6 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
              <div className="sm:col-span-2 lg:col-span-4">
                <label htmlFor="destination" className="label">
                  Destination
                </label>
                <input
                  id="destination"
                  required
                  maxLength={200}
                  autoFocus
                  value={destination}
                  onChange={(e) => setDestination(e.target.value)}
                  placeholder="Goa, Jaipur, Kerala…"
                  className="input py-3 text-base"
                />
              </div>

              <div>
                <label htmlFor="start-date" className="label">
                  Start date
                </label>
                <input
                  id="start-date"
                  required
                  type="date"
                  min={today()}
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  className="input"
                />
              </div>
              <div>
                <label htmlFor="end-date" className="label">
                  End date {nights > 0 && <span className="font-normal text-ink-500">· {plural(nights, "night")}</span>}
                </label>
                <input
                  id="end-date"
                  required
                  type="date"
                  min={startDate ? dayAfter(startDate) : today()}
                  max={startDate ? dayAfter(startDate, MAX_TRIP_NIGHTS) : undefined}
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  className="input"
                />
              </div>

              <div>
                <label htmlFor="budget" className="label">
                  Budget <span className="font-normal text-ink-500">· whole trip, everyone</span>
                </label>
                <div className="relative">
                  <span className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-sm text-ink-500" aria-hidden>
                    ₹
                  </span>
                  <input
                    id="budget"
                    required
                    type="number"
                    inputMode="numeric"
                    min="1000"
                    step="any"
                    value={budget}
                    onChange={(e) => setBudget(e.target.value)}
                    placeholder="50000"
                    className="input pl-8"
                  />
                </div>
              </div>

              <div>
                <label htmlFor="group-size" className="label">
                  Travellers
                </label>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setGroupSize((n) => Math.max(1, n - 1))}
                    disabled={groupSize <= 1}
                    aria-label="One fewer"
                    className="btn-secondary h-[42px] w-[42px] shrink-0 p-0"
                  >
                    <Minus className="h-4 w-4" aria-hidden />
                  </button>
                  <input
                    id="group-size"
                    type="number"
                    inputMode="numeric"
                    min="1"
                    max="9"
                    required
                    value={groupSize}
                    onChange={(e) => setGroupSize(Math.min(9, Math.max(1, parseInt(e.target.value, 10) || 1)))}
                    className="input text-center"
                  />
                  <button
                    type="button"
                    onClick={() => setGroupSize((n) => Math.min(9, n + 1))}
                    disabled={groupSize >= 9}
                    aria-label="One more"
                    className="btn-secondary h-[42px] w-[42px] shrink-0 p-0"
                  >
                    <Plus className="h-4 w-4" aria-hidden />
                  </button>
                </div>
              </div>

              <div className="sm:col-span-2 lg:col-span-4">
                <label htmlFor="interests" className="label">
                  Interests <span className="font-normal text-ink-500">· optional, comma-separated</span>
                </label>
                <input
                  id="interests"
                  value={interests}
                  onChange={(e) => setInterests(e.target.value)}
                  placeholder="beach, history, food"
                  className="input"
                />
                <div className="mt-2.5 flex flex-wrap gap-1.5" role="group" aria-label="Suggestions">
                  {INTEREST_IDEAS.map((idea) => {
                    const on = chosen.includes(idea);
                    return (
                      <button
                        key={idea}
                        type="button"
                        onClick={() => toggleInterest(idea)}
                        aria-pressed={on}
                        className={`focus-ring rounded-full border px-3 py-1 text-xs font-medium capitalize transition-colors ${
                          on ? "border-ink-900 bg-ink-900 text-white" : "border-ink-200 bg-white text-ink-600 hover:border-ink-300 hover:bg-ink-50"
                        }`}
                      >
                        {idea}
                      </button>
                    );
                  })}
                </div>
              </div>

              {formError && (
                <p role="alert" className="rounded-xl bg-bad-soft px-3.5 py-2.5 text-sm text-bad-ink sm:col-span-2 lg:col-span-4">
                  {formError}
                </p>
              )}

              <div className="flex gap-3 sm:col-span-2 lg:col-span-4">
                <button type="submit" disabled={creating} className="btn-primary px-5">
                  {creating ? (
                    <>
                      <Spinner /> Creating…
                    </>
                  ) : (
                    <>
                      Create &amp; plan
                      <ArrowRight className="h-4 w-4" aria-hidden />
                    </>
                  )}
                </button>
                <button type="button" onClick={() => setShowNew(false)} className="btn-ghost">
                  Cancel
                </button>
              </div>
            </form>
          </section>
        )}

        {/* Trips */}
        {loading ? (
          <div className="mt-10 grid gap-5 sm:grid-cols-2 xl:grid-cols-3" aria-hidden>
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton h-72" />
            ))}
          </div>
        ) : trips.length === 0 ? (
          !showNew && (
            <div className="card mt-10 flex flex-col items-center px-6 py-20 text-center">
              <LogoMark size={56} />
              <h2 className="mt-6 font-display text-2xl font-medium tracking-tight text-ink-900">No trips yet</h2>
              <p className="mt-2 max-w-sm text-sm text-ink-600">
                Give it a destination, dates and a budget. It finds the flights, a place to stay and things to do.
              </p>
              <button onClick={() => setShowNew(true)} className="btn-primary mt-7">
                <Plus className="h-4 w-4" aria-hidden />
                Plan a trip
              </button>
            </div>
          )
        ) : (
          <>
            {/* Search (Phase 23): by what a trip is about, among the trips that have a plan */}
            <form role="search" onSubmit={handleSearch} className="mt-8 flex gap-2">
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" aria-hidden />
                <input
                  type="search"
                  value={query}
                  // emptying the box — by hand or with its own ✕ — goes back to every trip
                  onChange={(e) => (e.target.value ? setQuery(e.target.value) : showAllTrips())}
                  maxLength={200}
                  aria-label="Search your trips"
                  placeholder="Search your trips — “beaches”, “forts and palaces”…"
                  className="input pl-10"
                />
              </div>
              <button type="submit" disabled={searching} className="btn-secondary shrink-0">
                {searching ? <Spinner /> : <Search className="h-4 w-4 sm:hidden" aria-hidden />}
                <span className={searching ? "" : "sr-only sm:not-sr-only"}>{searching ? "Searching…" : "Search"}</span>
              </button>
            </form>
            {searchError && (
              <p role="alert" className="mt-3 rounded-lg bg-bad-soft px-3 py-2 text-sm text-bad-ink">
                {searchError}
              </p>
            )}

            {found ? (
              <section aria-label="Search results" className="mt-8">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p role="status" className="text-sm text-ink-600">
                    {found.matches.length > 0
                      ? `${plural(found.matches.length, "trip")} ${found.matches.length === 1 ? "matches" : "match"} “${found.query}”, the closest first.`
                      : `No trips match “${found.query}”.`}
                  </p>
                  <button onClick={showAllTrips} className="btn-ghost px-3 py-1.5">
                    <X className="h-4 w-4" aria-hidden />
                    Show all trips
                  </button>
                </div>
                {found.matches.length > 0 ? (
                  <ul className="mt-4 grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
                    {found.matches.map((match) => (
                      <li key={match.trip.id}>
                        <TripCard trip={match.trip} match={match} />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="card mt-4 px-6 py-10 text-center text-sm text-ink-600">
                    It searches the trips that have a plan, by what they are about — try a place, or what you did there.
                  </p>
                )}
              </section>
            ) : (
              <ul className="mt-8 grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
                {trips.map((trip) => (
                  <li key={trip.id}>
                    <TripCard trip={trip} />
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </main>
    </div>
  );
}
