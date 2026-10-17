/**
 * Phase 36 — the languages a plan can be written in.
 *
 * The planner writes a plan in Hindi, Tamil or Bengali when the traveller has chosen it
 * (`preferred_language` in their preferences) or wrote the request in it. What is then in that
 * language — a stop's local name and description, the local tips — is marked with its `lang`, so
 * that the browser picks a font for its script and a screen reader reads it in the right voice.
 * Everything around it stays English: the page's own words are not translated.
 *
 * The list is src/ai/language.py's SUPPORTED (a test keeps the two in step).
 */

export interface PlanLanguageInfo {
  code: "hi" | "ta" | "bn";
  name: string; // in English
  native: string; // in itself
}

export const PLAN_LANGUAGES: readonly PlanLanguageInfo[] = [
  { code: "hi", name: "Hindi", native: "हिन्दी" },
  { code: "ta", name: "Tamil", native: "தமிழ்" },
  { code: "bn", name: "Bengali", native: "বাংলা" },
];

/** What a traveller can choose: a language, English, or none ("as I write" — each request's own decides). */
export type PreferredLanguage = PlanLanguageInfo["code"] | "en" | null;

/** The language a plan is written in, when it is one of the three — null for English, and for anything else. */
export function planLanguage(code: unknown): PlanLanguageInfo | null {
  return PLAN_LANGUAGES.find((language) => language.code === code) ?? null;
}
