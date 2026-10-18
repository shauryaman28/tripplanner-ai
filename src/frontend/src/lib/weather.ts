/**
 * Phase 38 — a day's weather, as each day of an itinerary carries it.
 *
 * Set by the planner (src/ai/weather.py): the condition, its kind — sunny, cloudy, rainy or snowy — the
 * day's high and low, and whether it is an estimate. OpenWeatherMap forecasts five days; past them the
 * planner gives the month's typical day for the places it knows, and says so — the page shows it as a
 * "climate estimate". A place it does not know has no weather past the forecast, and its days show none.
 */

import type { DayWeather } from "./types";

export const WEATHER_KINDS = ["sunny", "cloudy", "rainy", "snowy"] as const;
export type WeatherKind = (typeof WEATHER_KINDS)[number];

/** What each kind is called on the page (src/backend/app/pdf/plan.py says it the same — a test keeps them alike). */
export const WEATHER_LABELS: Record<WeatherKind, string> = {
  sunny: "Sunny",
  cloudy: "Partly cloudy",
  rainy: "Rain",
  snowy: "Snow",
};

/** The day's weather in words: "Rain · 29° / 23°". Null for a day without any. */
export function weatherWords(weather: DayWeather | null | undefined): string | null {
  if (!weather) return null;
  const label = WEATHER_LABELS[weather.kind as WeatherKind] ?? weather.condition;
  const temps =
    weather.temp_high_c != null && weather.temp_low_c != null
      ? ` · ${Math.round(weather.temp_high_c)}° / ${Math.round(weather.temp_low_c)}°`
      : "";
  return `${label}${temps}`;
}
