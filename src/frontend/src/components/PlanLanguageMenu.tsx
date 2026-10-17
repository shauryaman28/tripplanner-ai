"use client";

import { Languages } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { getPreferences, getToken, savePreferredLanguage } from "@/lib/api";
import { PLAN_LANGUAGES, type PreferredLanguage } from "@/lib/language";

type Saving = "idle" | "saving" | "saved" | "failed";

/**
 * The language the traveller's plans are written in (Phase 36). Saved with their preferences, on the
 * server: it holds on every device, and after signing out and in again. "As I write" is none chosen —
 * each request is planned in the language it is written in.
 */
export default function PlanLanguageMenu() {
  const id = useId();
  const [chosen, setChosen] = useState<PreferredLanguage | undefined>(undefined); // undefined: not loaded yet
  const [saving, setSaving] = useState<Saving>("idle");

  useEffect(() => {
    if (!getToken()) return;
    let live = true;
    getPreferences()
      .then((saved) => live && setChosen(saved.preferred_language ?? null))
      .catch(() => live && setChosen(null));
    return () => {
      live = false;
    };
  }, []);

  async function choose(next: PreferredLanguage) {
    const before = chosen ?? null;
    setChosen(next);
    setSaving("saving");
    try {
      setChosen((await savePreferredLanguage(next)).preferred_language ?? null);
      setSaving("saved");
    } catch {
      setChosen(before);
      setSaving("failed");
    }
  }

  if (chosen === undefined) return null;

  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <label htmlFor={id} className="flex items-center gap-1.5 text-ink-500" title="The language your plans are written in">
        <Languages className="h-4 w-4" aria-hidden />
        <span className="sr-only">Plan language</span>
      </label>
      <select
        id={id}
        value={chosen ?? ""}
        onChange={(event) => choose((event.target.value || null) as PreferredLanguage)}
        disabled={saving === "saving"}
        data-plan-language={chosen ?? ""}
        className="focus-ring max-w-[9.5rem] cursor-pointer rounded-lg border border-transparent bg-transparent py-1.5 pl-1.5 pr-6 text-sm text-ink-700 transition hover:border-ink-200 hover:bg-white disabled:cursor-wait disabled:opacity-60"
      >
        <option value="">As I write</option>
        <option value="en">English</option>
        {PLAN_LANGUAGES.map((language) => (
          <option key={language.code} value={language.code} lang={language.code}>
            {language.native} · {language.name}
          </option>
        ))}
      </select>
      <span role="status" className={saving === "failed" ? "text-xs font-medium text-bad-ink" : "sr-only"}>
        {saving === "failed" ? "Not saved" : saving === "saved" ? "Plan language saved" : ""}
      </span>
    </div>
  );
}
