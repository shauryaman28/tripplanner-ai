"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { listTrips, createTrip, getToken } from "@/lib/api";
import type { Trip } from "@/lib/types";

function TripStatusBadge({ status }: { status: Trip["status"] }) {
  const styles: Record<Trip["status"], string> = {
    pending:   "bg-gray-100 text-gray-600",
    planning:  "bg-yellow-50 text-yellow-700",
    completed: "bg-green-50 text-green-700",
    failed:    "bg-red-50 text-red-600",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${styles[status]}`}>
      {status}
    </span>
  );
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

function formatCurrency(amount: number) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(amount);
}

export default function TripsPage() {
  const router = useRouter();
  const [trips, setTrips] = useState<Trip[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);

  // New trip form state
  const [destination, setDestination] = useState("");
  const [startDate, setStartDate]     = useState("");
  const [endDate, setEndDate]         = useState("");
  const [budget, setBudget]           = useState("");
  const [groupSize, setGroupSize]     = useState("1");
  const [interests, setInterests]     = useState("");
  const [creating, setCreating]       = useState(false);
  const [formError, setFormError]     = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    listTrips()
      .then(setTrips)
      .catch(() => router.replace("/login"))
      .finally(() => setLoading(false));
  }, [router]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setCreating(true);
    try {
      const trip = await createTrip({
        destination,
        start_date: startDate,
        end_date: endDate,
        budget: parseFloat(budget),
        group_size: parseInt(groupSize, 10),
        interests: interests ? interests.split(",").map((s) => s.trim()).filter(Boolean) : undefined,
      });
      router.push(`/trips/${trip.id}`);
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : "Failed to create trip");
      setCreating(false);
    }
  }

  if (loading) {
    return (
      <main className="flex min-h-dvh items-center justify-center">
        <span className="text-sm text-muted">Loading…</span>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <h1 className="font-display text-2xl font-bold text-gray-900">My trips</h1>
        <button onClick={() => setShowNew(true)} className="btn-primary">
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
            <path d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z" />
          </svg>
          Plan a trip
        </button>
      </div>

      {/* New trip form */}
      {showNew && (
        <div className="card mb-6 p-5 animate-slide-up">
          <h2 className="mb-4 font-display text-lg font-semibold text-gray-900">New trip</h2>
          <form onSubmit={handleCreate} className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label className="mb-1 block text-xs font-medium text-gray-600">Destination</label>
              <input
                required
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder="Goa, Jaipur, Kerala…"
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Start date</label>
              <input
                required type="date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">End date</label>
              <input
                required type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Budget (INR)</label>
              <input
                required type="number" min="1000"
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                placeholder="50000"
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-600">Group size</label>
              <input
                type="number" min="1" max="20"
                value={groupSize}
                onChange={(e) => setGroupSize(e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>
            <div className="sm:col-span-2">
              <label className="mb-1 block text-xs font-medium text-gray-600">
                Interests <span className="text-muted font-normal">(comma-separated)</span>
              </label>
              <input
                value={interests}
                onChange={(e) => setInterests(e.target.value)}
                placeholder="beach, history, food"
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus-ring outline-none"
              />
            </div>

            {formError && (
              <p role="alert" className="sm:col-span-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600">
                {formError}
              </p>
            )}

            <div className="sm:col-span-2 flex gap-3">
              <button type="submit" disabled={creating} className="btn-primary">
                {creating ? "Creating…" : "Create & plan"}
              </button>
              <button type="button" onClick={() => setShowNew(false)} className="btn-ghost">
                Cancel
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Trips list */}
      {trips.length === 0 && !showNew ? (
        <div className="card flex flex-col items-center gap-3 py-16 text-center">
          <svg className="h-10 w-10 text-gray-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
          </svg>
          <p className="font-medium text-gray-500">No trips yet</p>
          <p className="text-sm text-muted">Create your first trip to get started.</p>
          <button onClick={() => setShowNew(true)} className="btn-primary mt-2">Plan a trip</button>
        </div>
      ) : (
        <ul className="space-y-3">
          {trips.map((trip) => (
            <li key={trip.id}>
              <button
                onClick={() => router.push(`/trips/${trip.id}`)}
                className="card w-full p-4 text-left hover:shadow-md transition-shadow active:scale-[0.99]"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-display font-semibold text-gray-900 truncate">{trip.destination}</p>
                    <p className="mt-0.5 text-sm text-muted">
                      {formatDate(trip.start_date)} — {formatDate(trip.end_date)}
                      {trip.group_size > 1 && ` · ${trip.group_size} people`}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1.5 shrink-0">
                    <TripStatusBadge status={trip.status} />
                    <span className="text-sm font-medium text-gray-700">{formatCurrency(trip.budget)}</span>
                  </div>
                </div>
                {trip.interests && trip.interests.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {trip.interests.slice(0, 4).map((i) => (
                      <span key={i} className="rounded-full bg-brand-50 px-2 py-0.5 text-xs text-brand-700">
                        {i}
                      </span>
                    ))}
                    {trip.interests.length > 4 && (
                      <span className="text-xs text-muted">+{trip.interests.length - 4}</span>
                    )}
                  </div>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
