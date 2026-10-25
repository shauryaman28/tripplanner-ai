"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Mail } from "lucide-react";
import { useEffect, useState } from "react";

import AppHeader from "@/components/AppHeader";
import { Spinner } from "@/components/ui";
import { getEmail, getPreferences, getToken, saveNotifications } from "@/lib/api";
import type { Preferences } from "@/lib/types";

type Saving = "idle" | "saving" | "saved" | "failed";
type Emails = Pick<Preferences, "notification_email" | "reminder_hours_before">;

/** How long before a trip begins its reminder comes — the backend takes any hour from 1 to 168. */
const REMINDER_CHOICES = [3, 6, 12, 24, 48, 72, 168];

function before(hours: number): string {
  if (hours === 168) return "A week before";
  if (hours % 24 === 0) return hours === 24 ? "A day before" : `${hours / 24} days before`;
  return hours === 1 ? "An hour before" : `${hours} hours before`;
}

/**
 * Phase 40 — the emails a traveller is sent: "Your itinerary is ready" when a trip's plan is made, and a
 * reminder before the trip begins (before its flight leaves, or the morning of its first day). Saved with their
 * preferences, on the server: the backend reads them when it is about to send.
 */
export default function SettingsPage() {
  const router = useRouter();
  const [saved, setSaved] = useState<Emails | null>(null); // null: not loaded yet
  const [form, setForm] = useState<Emails | null>(null);
  const [saving, setSaving] = useState<Saving>("idle");
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setEmail(getEmail());
    // only the last load fills the form: an earlier one answering late would undo what the traveller changed
    let live = true;
    getPreferences()
      .then(({ notification_email, reminder_hours_before }) => {
        if (!live) return;
        setSaved({ notification_email, reminder_hours_before });
        setForm({ notification_email, reminder_hours_before });
      })
      .catch(() => live && router.replace("/login"));
    return () => {
      live = false;
    };
  }, [router]);

  function change(next: Partial<Emails>) {
    setForm((current) => (current ? { ...current, ...next } : current));
    setSaving("idle");
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!form) return;
    setSaving("saving");
    try {
      const { notification_email, reminder_hours_before } = await saveNotifications(form);
      setSaved({ notification_email, reminder_hours_before });
      setForm({ notification_email, reminder_hours_before });
      setSaving("saved");
    } catch {
      setSaving("failed");
    }
  }

  const changed =
    !!form && !!saved && (form.notification_email !== saved.notification_email || form.reminder_hours_before !== saved.reminder_hours_before);
  const choices = form && !REMINDER_CHOICES.includes(form.reminder_hours_before)
    ? [...REMINDER_CHOICES, form.reminder_hours_before].sort((a, b) => a - b) // one set some other way still shows
    : REMINDER_CHOICES;

  return (
    <div className="min-h-dvh">
      <AppHeader />

      <main className="mx-auto max-w-2xl px-4 pb-16 pt-10 sm:px-6">
        <Link href="/trips" className="inline-flex items-center gap-1.5 text-sm text-ink-600 hover:text-ink-900">
          <ArrowLeft className="h-4 w-4" aria-hidden />
          Your trips
        </Link>
        <h1 className="mt-4 font-display text-4xl font-medium tracking-tight text-ink-900">Settings</h1>

        {!form ? (
          <p className="mt-8 flex items-center gap-2 text-sm text-ink-600">
            <Spinner /> Loading your settings…
          </p>
        ) : (
          <form onSubmit={save} aria-label="Email notifications" className="card mt-8 p-5 sm:p-7">
            <div className="flex items-start gap-3">
              <Mail className="mt-0.5 h-5 w-5 shrink-0 text-ink-500" aria-hidden />
              <div className="min-w-0 flex-1">
                <h2 className="font-medium text-ink-900">Email notifications</h2>
                <p className="mt-1 text-sm text-ink-600">
                  An email when your itinerary is ready, and a reminder before your trip begins
                  {email ? (
                    <>
                      {" "}
                      — to <span className="font-medium text-ink-800">{email}</span>
                    </>
                  ) : null}
                  .
                </p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={form.notification_email}
                aria-label="Email notifications"
                onClick={() => change({ notification_email: !form.notification_email })}
                className={`focus-ring relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition ${
                  form.notification_email ? "bg-ink-900" : "bg-ink-200"
                }`}
              >
                <span
                  aria-hidden
                  className={`inline-block h-5 w-5 rounded-full bg-white shadow transition ${
                    form.notification_email ? "translate-x-5" : "translate-x-0.5"
                  }`}
                />
              </button>
            </div>

            <div className="mt-6 sm:pl-8">
              <label htmlFor="reminder" className="label">
                Trip reminder
              </label>
              <select
                id="reminder"
                value={form.reminder_hours_before}
                onChange={(event) => change({ reminder_hours_before: Number(event.target.value) })}
                disabled={!form.notification_email}
                className="input max-w-xs cursor-pointer disabled:cursor-not-allowed disabled:opacity-50"
              >
                {choices.map((hours) => (
                  <option key={hours} value={hours}>
                    {before(hours)}
                  </option>
                ))}
              </select>
              <p className="mt-1.5 text-xs text-ink-500">
                {form.notification_email
                  ? "Before your flight leaves — or 8 in the morning on your first day, when there is no flight."
                  : "No emails are sent while notifications are off."}
              </p>
            </div>

            <div className="mt-7 flex items-center gap-3 sm:pl-8">
              <button type="submit" disabled={!changed || saving === "saving"} className="btn-primary px-5">
                {saving === "saving" && <Spinner />}
                Save
              </button>
              <span
                role="status"
                className={saving === "failed" ? "text-sm font-medium text-bad-ink" : "text-sm text-ink-600"}
              >
                {saving === "saved" ? "Saved" : saving === "failed" ? "Not saved — try again" : ""}
              </span>
            </div>
          </form>
        )}
      </main>
    </div>
  );
}
