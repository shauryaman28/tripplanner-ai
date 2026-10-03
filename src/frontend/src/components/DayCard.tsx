"use client";

import { BedDouble, Footprints, Landmark, Moon, PlaneLanding, Sun, Sunrise, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { flightPart, stayPart, stopPart, type PlanSection } from "@/lib/changes";
import { formatClock, formatDuration, formatINR, formatWeekday } from "@/lib/format";
import { HOTEL_STYLE, SLOT_LABELS, dayStops, dayStyle, hotelPinId, type DayStop, type PinStyle, type SlotName } from "@/lib/map";
import { categoryMeta, describeRating } from "@/lib/places";
import type { DaySchedule } from "@/lib/types";
import { Pin, UpdatedTag, UpdatingTag, partClass } from "./ui";

interface Props {
  day: DaySchedule;
  /** Scroll to the map and open this pin. */
  onShowOnMap: (pinId: string) => void;
  /** The section a run is working on right now: its rows show it, the others stay as they are. */
  updating?: PlanSection | null;
  /** The parts that came back different from the last change (lib/changes.ts) — marked for a few seconds. */
  changed?: ReadonlySet<string>;
}

const NOTHING_CHANGED: ReadonlySet<string> = new Set();

const SLOT_ICONS: Record<SlotName, LucideIcon> = { morning: Sunrise, afternoon: Sun, evening: Moon };

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
}: {
  day: number;
  stop: DayStop;
  onShowOnMap: Props["onShowOnMap"];
  updating: boolean;
  changed: boolean;
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
        <p className="mt-0.5 text-xs text-ink-500">Nothing booked — explore the area at your own pace.</p>
      </Row>
    );
  }

  const category = categoryMeta(activity.category);
  const rating = describeRating(activity.rating);

  return (
    <Row
      icon={SLOT_ICONS[slot]}
      tone="bg-ink-100 text-ink-600"
      eyebrow={SLOT_LABELS[slot]}
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
      </div>
    </Row>
  );
}

export default function DayCard({ day, onShowOnMap, updating = null, changed = NOTHING_CHANGED }: Props) {
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
          </div>
          {dayCost > 0 && (
            <p className="text-right">
              <span className="block text-sm font-semibold text-ink-900">{formatINR(dayCost)}</span>
              <span className="block text-[11px] text-ink-500">stay + activities</span>
            </p>
          )}
        </header>

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
            </Row>
          )}

          {stops.map((stop) => (
            <ActivityRow
              key={stop.id}
              day={day.day}
              stop={stop}
              onShowOnMap={onShowOnMap}
              updating={updating === "activities"}
              changed={changed.has(stopPart(day.day, stop.slot))}
            />
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
            </Row>
          )}
        </ul>
      </div>
    </article>
  );
}
