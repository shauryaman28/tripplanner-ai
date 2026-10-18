"use client";

import {
  BedDouble,
  CarTaxiFront,
  CloudRain,
  CloudSun,
  Footprints,
  Landmark,
  Moon,
  PlaneLanding,
  Snowflake,
  Sun,
  Sunrise,
  Umbrella,
  Users,
  type LucideIcon,
} from "lucide-react";
import { Fragment, type ReactNode } from "react";

import { flightPart, stayPart, stopPart, type PlanSection } from "@/lib/changes";
import { formatClock, formatDuration, formatINR, formatWeekday, namesInWords } from "@/lib/format";
import { HOTEL_STYLE, SLOT_LABELS, dayStops, dayStyle, hotelPinId, type DayStop, type PinStyle, type SlotName } from "@/lib/map";
import { categoryMeta, describeRating } from "@/lib/places";
import { stopTimes, travelWords } from "@/lib/schedule";
import { weatherWords, type WeatherKind } from "@/lib/weather";
import type { ActivitySlot, DaySchedule } from "@/lib/types";
import BookButton from "./BookButton";
import { Pin, UpdatedTag, UpdatingTag, partClass } from "./ui";

interface Props {
  day: DaySchedule;
  /** Scroll to the map and open this pin. */
  onShowOnMap: (pinId: string) => void;
  /** The section a run is working on right now: its rows show it, the others stay as they are. */
  updating?: PlanSection | null;
  /** The parts that came back different from the last change (lib/changes.ts) — marked for a few seconds. */
  changed?: ReadonlySet<string>;
  /** Phase 36: the language the plan is written in ("hi", "ta", "bn") — what is in it is marked with it. */
  language?: string;
  /** Phase 39: the first night of this stay — the card that carries "Book hotel" (the others need not repeat it). */
  firstNight?: boolean;
}

const NOTHING_CHANGED: ReadonlySet<string> = new Set();

const SLOT_ICONS: Record<SlotName, LucideIcon> = { morning: Sunrise, afternoon: Sun, evening: Moon };
const WEATHER_ICONS: Record<WeatherKind, LucideIcon> = { sunny: Sun, cloudy: CloudSun, rainy: CloudRain, snowy: Snowflake };

function Row({
  icon: Icon,
  tone,
  eyebrow,
  title,
  aside,
  children,
  part,
  updating = false,
  changed = false,
}: {
  icon: LucideIcon;
  tone: string;
  eyebrow: string;
  title: ReactNode;
  aside?: ReactNode;
  children?: ReactNode;
  /** What this row is, for a change request: "flight", "stay:2", "stop:1-morning". */
  part?: string;
  /** A run is working on this row's section. */
  updating?: boolean;
  /** This row came back different from the last change. */
  changed?: boolean;
}) {
  return (
    <li data-part={part} data-updating={updating || undefined} data-changed={changed || undefined} className="py-1.5">
      {/* the row sits in a box of its own, a little wider than its content: room for the "changed" tint */}
      <div className={`-mx-2 flex gap-3.5 rounded-xl px-2 py-2 ${partClass(updating, changed)}`}>
        <span className={`mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-xl ${tone}`}>
          <Icon className="h-[18px] w-[18px]" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="eyebrow flex flex-wrap items-center gap-x-2 gap-y-1">
            {eyebrow}
            {changed && <UpdatedTag />}
          </p>
          <p className="mt-0.5 text-[15px] font-medium leading-snug text-ink-900">{title}</p>
          {children}
        </div>
        {updating ? <UpdatingTag /> : aside}
      </div>
    </li>
  );
}

/** Phase 36 — a stop's name and a sentence on it, in the plan's language. Nothing for a plan in English. */
function InTheirLanguage({ activity, language }: { activity: ActivitySlot; language?: string }) {
  if (!language || !(activity.local_name || activity.description)) return null;
  return (
    <div lang={language} data-local-text className="mt-1 space-y-0.5">
      {activity.local_name && <p className="text-sm font-medium text-ink-700">{activity.local_name}</p>}
      {activity.description && <p className="break-words text-sm leading-relaxed text-ink-600">{activity.description}</p>}
    </div>
  );
}

/** The stop's own pin — same colour, shape and number as on the map; click to go there. */
function MapButton({ pinStyle, label, name, onClick }: { pinStyle: PinStyle; label: ReactNode; name: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-label={`Show ${name} on the map`}
      className="focus-ring flex shrink-0 items-center gap-1.5 self-center rounded-full py-1 pl-1 pr-2.5 text-xs font-medium text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-900"
    >
      <Pin pinStyle={pinStyle} size="sm">
        {label}
      </Pin>
      Map
    </button>
  );
}

function Popularity({ level }: { level: number }) {
  return (
    <span className="flex gap-0.5" aria-hidden>
      {[1, 2, 3].map((dot) => (
        <span key={dot} className={`h-1.5 w-1.5 rounded-full ${dot <= level ? "bg-ink-700" : "bg-ink-200"}`} />
      ))}
    </span>
  );
}

function ActivityRow({
  day,
  stop,
  onShowOnMap,
  updating,
  changed,
  language,
}: {
  day: number;
  stop: DayStop;
  onShowOnMap: Props["onShowOnMap"];
  updating: boolean;
  changed: boolean;
  language?: string;
}) {
  const { activity, slot } = stop;

  // Free time only ever stands alone (see dayStops): it is the whole day, not a slot.
  if (stop.freeTime) {
    return (
      <Row
        icon={Footprints}
        tone="bg-ink-50 text-ink-400"
        eyebrow="All day"
        title={<span className="text-ink-600">Free time</span>}
        part={stopPart(day, slot)}
        updating={updating}
      >
        {activity.no_time_left ? (
          // Phase 37: the flight lands too late for anything that day
          <p className="mt-0.5 text-xs text-ink-500">You arrive late — nothing is planned for today.</p>
        ) : language && activity.description ? (
          <p lang={language} data-local-text className="mt-0.5 text-sm text-ink-600">
            {activity.description}
          </p>
        ) : (
          <p className="mt-0.5 text-xs text-ink-500">Nothing booked — explore the area at your own pace.</p>
        )}
      </Row>
    );
  }

  const category = categoryMeta(activity.category);
  const rating = describeRating(activity.rating);
  const times = stopTimes(activity); // Phase 37

  return (
    <Row
      icon={SLOT_ICONS[slot]}
      tone="bg-ink-100 text-ink-600"
      eyebrow={times ? `${SLOT_LABELS[slot]} · ${times}` : SLOT_LABELS[slot]}
      title={activity.activity}
      part={stopPart(day, slot)}
      updating={updating}
      changed={changed}
      aside={
        stop.order !== null && (
          <MapButton pinStyle={dayStyle(day)} label={stop.order} name={activity.activity} onClick={() => onShowOnMap(stop.id)} />
        )
      }
    >
      <InTheirLanguage activity={activity} language={language} />
      {activity.short_visit && (
        <p className="mt-1 text-xs font-medium text-warn-ink">A short visit — getting here takes much of this part of the day.</p>
      )}
      {activity.no_time_left && (
        <p className="mt-1 text-xs font-medium text-warn-ink">No time left for this today: the day is over by the time you can be here.</p>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-600">
        {category && (
          <span className="flex items-center gap-1">
            <category.icon className="h-3.5 w-3.5 text-ink-400" aria-hidden />
            {category.label}
          </span>
        )}
        {rating && (
          <span className="flex items-center gap-1.5">
            <Popularity level={rating.level} />
            {rating.label}
          </span>
        )}
        {rating?.heritage && (
          <span className="flex items-center gap-1">
            <Landmark className="h-3.5 w-3.5 text-ink-400" aria-hidden />
            Heritage site
          </span>
        )}
        {activity.cost > 0 && <span>{formatINR(activity.cost)}</span>}
        {stop.order === null && <span className="text-ink-500">No map location for this place</span>}
        {/* Phase 25, a group trip: whose interests this stop answers */}
        {activity.suits && activity.suits.length > 0 && (
          <span data-suits={activity.suits.join("|")} className="flex items-center gap-1 font-medium text-ink-700">
            <Users className="h-3.5 w-3.5 text-ink-400" aria-hidden />
            For {namesInWords(activity.suits)}
          </span>
        )}
      </div>
    </Row>
  );
}

/** Phase 38 — the day's weather under its date: an icon, the words, and "climate estimate" past the forecast. */
function DayWeatherLine({ day }: { day: DaySchedule }) {
  const words = weatherWords(day.weather);
  if (!day.weather || !words) return null;
  const Icon = WEATHER_ICONS[day.weather.kind] ?? CloudSun;
  return (
    <p data-weather={day.weather.kind} className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-ink-600">
      <Icon className="h-4 w-4 text-ink-500" aria-hidden />
      <span>{words}</span>
      {day.weather.estimate && (
        <span
          data-climate-estimate
          className="rounded-full bg-ink-100 px-2 py-0.5 text-[11px] font-medium text-ink-600"
          title="Past the five-day forecast: a typical day of the month here"
        >
          climate estimate
        </span>
      )}
    </p>
  );
}

export default function DayCard({
  day,
  onShowOnMap,
  updating = null,
  changed = NOTHING_CHANGED,
  language,
  firstNight = true,
}: Props) {
  const stops = dayStops(day);
  const { hotel, flight } = day;
  const dayCost = stops.reduce((sum, stop) => sum + (stop.activity.cost ?? 0), 0) + (hotel?.cost_per_night ?? 0);

  const route = [flight?.origin?.code, flight?.destination?.code].filter(Boolean).join(" → ");
  const times = [formatClock(flight?.departure), formatClock(flight?.arrival)].filter(Boolean).join(" – ");
  const flightFacts = [
    flight?.flight_number ?? flight?.airline,
    times,
    formatDuration(flight?.duration_mins),
    flight?.stops === 0 ? "Non-stop" : flight?.stops ? `${flight.stops} stop${flight.stops > 1 ? "s" : ""}` : null,
  ].filter(Boolean);

  const hotelFacts = [
    hotel?.stars ? `${hotel.stars}-star` : null,
    hotel?.rating ? `${hotel.rating}/10 guest rating` : null,
    hotel?.address,
  ].filter(Boolean);

  return (
    <article className="card flex animate-rise overflow-hidden">
      {/* the day's colour — the same one its pins and route wear on the map */}
      <div className="w-1.5 shrink-0" style={{ background: dayStyle(day.day).color }} aria-hidden />

      <div className="min-w-0 flex-1 px-5 pb-2 pt-4">
        <header className="flex items-baseline justify-between gap-4">
          <div>
            <p className="eyebrow">Day {day.day}</p>
            <h3 className="mt-0.5 font-display text-xl font-medium text-ink-900">{formatWeekday(day.date)}</h3>
            <DayWeatherLine day={day} />
          </div>
          {dayCost > 0 && (
            <p className="text-right">
              <span className="block text-sm font-semibold text-ink-900">{formatINR(dayCost)}</span>
              <span className="block text-[11px] text-ink-500">stay + activities</span>
            </p>
          )}
        </header>

        {/* Phase 38: rain forecast, and nothing indoors to give the day */}
        {day.weather_note && (
          <p data-weather-note className="mt-2 flex items-start gap-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn-ink">
            <Umbrella className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
            {day.weather_note}
          </p>
        )}

        <ul className="mt-1 divide-y divide-ink-100">
          {flight && (
            <Row
              icon={PlaneLanding}
              tone="bg-ink-100 text-ink-700"
              eyebrow="Flight"
              title={route || flightFacts[0]}
              part={flightPart}
              updating={updating === "flights"}
              changed={changed.has(flightPart)}
              aside={
                flight.price_inr != null && (
                  <p className="shrink-0 self-center text-right">
                    <span className="block text-sm font-semibold text-ink-900">{formatINR(flight.price_inr)}</span>
                    <span className="block text-[11px] text-ink-500">return</span>
                  </p>
                )
              }
            >
              {flightFacts.length > 0 && <p className="mt-0.5 text-xs text-ink-600">{flightFacts.join(" · ")}</p>}
              <BookButton url={flight.booking_url} label="Book flight" site="Google Flights" />
            </Row>
          )}

          {stops.map((stop) => (
            <Fragment key={stop.id}>
              {/* Phase 37: the way to a stop more than 10 km from where the traveller was */}
              {stop.activity.travel && (
                <li data-travel={stop.id} className="flex items-center gap-3.5 py-2 pl-[3px] text-xs text-ink-600">
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-ink-50 text-ink-500">
                    {stop.activity.travel.mode === "walk" ? <Footprints className="h-3.5 w-3.5" aria-hidden /> : <CarTaxiFront className="h-3.5 w-3.5" aria-hidden />}
                  </span>
                  <span>{travelWords(stop.activity.travel)}</span>
                </li>
              )}
              <ActivityRow
                day={day.day}
                stop={stop}
                onShowOnMap={onShowOnMap}
                updating={updating === "activities"}
                changed={changed.has(stopPart(day.day, stop.slot))}
                language={language}
              />
            </Fragment>
          ))}

          {hotel && (
            <Row
              icon={BedDouble}
              tone="bg-saffron/25 text-ink-800"
              eyebrow="Stay"
              title={hotel.name}
              part={stayPart(day.day)}
              updating={updating === "stay"}
              changed={changed.has(stayPart(day.day))}
              aside={
                <div className="flex shrink-0 items-center gap-3 self-center">
                  <p className="hidden text-right sm:block">
                    <span className="block text-sm font-semibold text-ink-900">{formatINR(hotel.cost_per_night)}</span>
                    <span className="block text-[11px] text-ink-500">per night</span>
                  </p>
                  {typeof hotel.lat === "number" && typeof hotel.lng === "number" && (
                    <MapButton
                      pinStyle={HOTEL_STYLE}
                      label={<BedDouble className="h-3 w-3" />}
                      name={hotel.name}
                      onClick={() => onShowOnMap(hotelPinId(hotel.name))}
                    />
                  )}
                </div>
              }
            >
              {/* on a narrow screen the price moves under the name, which needs the width */}
              <p className="mt-0.5 text-xs text-ink-600 sm:hidden">
                <span className="text-sm font-semibold text-ink-900">{formatINR(hotel.cost_per_night)}</span> per night
              </p>
              {hotelFacts.length > 0 && <p className="mt-0.5 text-xs text-ink-600">{hotelFacts.join(" · ")}</p>}
              {firstNight && <BookButton url={hotel.booking_url} label="Book hotel" site="Booking.com" />}
            </Row>
          )}
        </ul>
      </div>
    </article>
  );
}
