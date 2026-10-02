"use client";

/**
 * Phase 18 — itinerary map (Leaflet).
 *
 * Each day has a colour; its stops are numbered in visiting order and joined
 * by a line of that colour. The hotel is gold and the flight's two airports
 * are neutral info markers. The legend doubles as a filter: pick a day to
 * isolate it. Loaded with `ssr: false` (Leaflet needs `window`) — see ItineraryView.
 */

import L from "leaflet";
import { BedDouble, Landmark, MapPinOff, Plane, Star, Ticket, type LucideIcon } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { MapContainer, Marker, Polyline, Popup, TileLayer, ZoomControl, useMap } from "react-leaflet";
import "leaflet/dist/leaflet.css";

import { formatINR, plural } from "@/lib/format";
import {
  AIRPORT_STYLE,
  HOTEL_STYLE,
  SLOT_LABELS,
  buildMapData,
  dayStyle,
  type LatLng,
  type PinStyle,
} from "@/lib/map";
import { categoryMeta, describeRating } from "@/lib/places";
import type { DaySchedule } from "@/lib/types";
import { Pin } from "./ui";

export interface MapFocus {
  /** a pin id from lib/map.ts */
  id: string;
  /** changes on every request, so asking for the same pin twice still works */
  nonce: number;
}

interface Props {
  days: DaySchedule[];
  /** Fly to this pin and open its popup. */
  focus: MapFocus | null;
}

// Tiles are configuration. The default is OpenStreetMap's own server, which needs no key
// (fine for development; a deployment should point these at a tile provider it has an account with).
const TILE_URL = process.env.NEXT_PUBLIC_MAP_TILE_URL ?? "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION =
  process.env.NEXT_PUBLIC_MAP_ATTRIBUTION ??
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const icon = (paths: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
const BED_ICON = icon('<path d="M2 20v-8a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v8"/><path d="M4 10V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v4"/><path d="M12 4v6"/><path d="M2 18h20"/>');
const PLANE_ICON = icon('<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/>');

/**
 * A pin drawn in HTML — the `.pin` mark from globals.css — so no marker image
 * has to be bundled. `inner` is only ever a number or one of the icons above,
 * never text from the API.
 */
function pinIcon(style: PinStyle, inner: string, kind: string): L.DivIcon {
  return L.divIcon({
    className: "",
    iconSize: [30, 30],
    iconAnchor: [15, 15],
    popupAnchor: [0, -17],
    html: `<span class="pin" data-pin="${kind}" data-shape="${style.shape}" style="--pin:${style.color};--pin-ink:${style.ink}"><span>${inner}</span></span>`,
  });
}

/** Frame these positions; refits whenever they change (e.g. a day is isolated). */
function FitTo({ positions }: { positions: LatLng[] }) {
  const map = useMap();
  useEffect(() => {
    if (positions.length === 1) map.setView(positions[0], 13);
    else if (positions.length > 1) map.fitBounds(L.latLngBounds(positions), { padding: [48, 48], maxZoom: 14 });
  }, [map, positions]);
  return null;
}

/** The page scrolls past the map untouched; the wheel zooms only once the map has been clicked. */
function WheelZoomOnceActive() {
  const map = useMap();
  useEffect(() => {
    const enable = () => map.scrollWheelZoom.enable();
    const disable = () => map.scrollWheelZoom.disable();
    map.on("click", enable);
    map.on("mouseout", disable);
    return () => {
      map.off("click", enable);
      map.off("mouseout", disable);
    };
  }, [map]);
  return null;
}

function FocusPin({ focus, markers }: { focus: MapFocus | null; markers: Map<string, L.Marker> }) {
  const map = useMap();
  useEffect(() => {
    const marker = focus ? markers.get(focus.id) : undefined;
    if (!marker) return;
    map.setView(marker.getLatLng(), Math.max(map.getZoom(), 13), { animate: true });
    marker.openPopup();
  }, [focus, map, markers]);
  return null;
}

function LegendButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`focus-ring flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
        active ? "border-ink-900 bg-ink-900 text-white" : "border-ink-200 bg-white text-ink-700 hover:border-ink-300 hover:bg-ink-50"
      }`}
    >
      {children}
    </button>
  );
}

function PopupRow({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <p className="flex items-start gap-2">
      <Icon className="mt-px h-3.5 w-3.5 shrink-0 text-ink-400" aria-hidden />
      <span>{children}</span>
    </p>
  );
}

function PopupCard({ eyebrow, title, children }: { eyebrow: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="p-4 pr-9 font-sans">
      <p className="eyebrow flex items-center gap-1.5">{eyebrow}</p>
      <p className="mt-1 text-[15px] font-semibold leading-snug text-ink-900">{title}</p>
      <div className="mt-2.5 space-y-1.5 text-xs text-ink-600">{children}</div>
    </div>
  );
}

export default function ItineraryMap({ days, focus }: Props) {
  const data = useMemo(() => buildMapData(days), [days]);
  const [isolatedDay, setIsolatedDay] = useState<number | null>(null);
  const [seenFocus, setSeenFocus] = useState<number | null>(null);
  const markers = useRef(new Map<string, L.Marker>()).current;
  const keep = (id: string) => (marker: L.Marker | null) => {
    if (marker) markers.set(id, marker);
    else markers.delete(id);
  };

  // A pin asked for from a day card must not stay dimmed by an older filter. Adjusting
  // state during render (not in an effect) keeps the refit and the fly-to in one commit.
  if (focus && focus.nonce !== seenFocus) {
    setSeenFocus(focus.nonce);
    setIsolatedDay(null);
  }
  // The filter must not point at a day a refinement has since removed.
  const activeDay = isolatedDay !== null && data.days.includes(isolatedDay) ? isolatedDay : null;
  const setActiveDay = setIsolatedDay;

  const icons = useMemo(
    () => ({
      activities: new Map(data.activities.map((pin) => [pin.id, pinIcon(dayStyle(pin.day), String(pin.order), `day-${pin.day}`)])),
      hotel: pinIcon(HOTEL_STYLE, BED_ICON, "hotel"),
      airport: pinIcon(AIRPORT_STYLE, PLANE_ICON, "airport"),
    }),
    [data.activities],
  );

  // Frame the destination: every pin except the far-away departure airport.
  const framed = useMemo<LatLng[]>(
    () =>
      activeDay !== null
        ? data.activities.filter((pin) => pin.day === activeDay).map((pin) => pin.position)
        : [
            ...data.activities.map((pin) => pin.position),
            ...data.hotels.map((pin) => pin.position),
            ...data.airports.filter((pin) => pin.role === "destination").map((pin) => pin.position),
          ],
    [data, activeDay],
  );
  const everything = useMemo<LatLng[]>(
    () => [...data.activities, ...data.hotels, ...data.airports].map((pin) => pin.position),
    [data],
  );

  const unmapped = data.unmapped.length > 0 && (
    <p className="flex items-start gap-2 border-t border-ink-200/70 px-5 py-3 text-xs text-ink-600">
      <MapPinOff className="mt-px h-3.5 w-3.5 shrink-0 text-ink-400" aria-hidden />
      <span>Not shown on the map (no location data): {data.unmapped.join("; ")}</span>
    </p>
  );

  if (everything.length === 0) {
    return (
      <section aria-label="Trip map" className="card">
        <div className="px-5 py-4">
          <h3 className="font-display text-xl font-medium text-ink-900">On the map</h3>
          <p className="mt-1 text-sm text-ink-600">None of this itinerary&apos;s places has a map location.</p>
        </div>
        {unmapped}
      </section>
    );
  }

  const dimmed = (day: number) => activeDay !== null && day !== activeDay;

  return (
    <section aria-label="Trip map" className="card overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 px-5 py-4">
        <div>
          <h3 className="font-display text-xl font-medium text-ink-900">On the map</h3>
          <p className="mt-0.5 text-xs text-ink-500">
            {plural(data.activities.length, "stop")}, numbered in visiting order. Select a pin for details.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Map legend">
          {data.days.length > 1 && (
            <LegendButton active={activeDay === null} onClick={() => setActiveDay(null)}>
              All days
            </LegendButton>
          )}
          {data.days.map((day) => (
            <LegendButton key={day} active={activeDay === day} onClick={() => setActiveDay(activeDay === day ? null : day)}>
              <Pin pinStyle={dayStyle(day)} size="xs" />
              Day {day}
            </LegendButton>
          ))}
          {data.hotels.length > 0 && (
            <span className="flex items-center gap-1.5 px-1.5 text-xs text-ink-600">
              <Pin pinStyle={HOTEL_STYLE} size="xs" />
              Hotel
            </span>
          )}
          {data.airports.length > 0 && (
            <span className="flex items-center gap-1.5 px-1.5 text-xs text-ink-600">
              <Pin pinStyle={AIRPORT_STYLE} size="xs" />
              Airport
            </span>
          )}
        </div>
      </div>

      {/* `isolate` keeps Leaflet's z-indexes below the sticky header */}
      <div className="tp-map isolate h-[420px] w-full border-t border-ink-200/70 sm:h-[500px]">
        <MapContainer center={everything[0]} zoom={11} scrollWheelZoom={false} zoomControl={false} className="h-full w-full">
          <TileLayer attribution={TILE_ATTRIBUTION} url={TILE_URL} />
          <ZoomControl position="bottomright" />
          <WheelZoomOnceActive />
          <FitTo positions={framed.length > 0 ? framed : everything} />
          <FocusPin focus={focus} markers={markers} />

          {data.routes.map((route) => {
            const faded = dimmed(route.day);
            return (
              <Fragment key={route.day}>
                {/* a white casing under the line lifts it off the basemap */}
                <Polyline positions={route.positions} pathOptions={{ color: "#ffffff", weight: 7, opacity: faded ? 0 : 0.9 }} interactive={false} />
                <Polyline
                  positions={route.positions}
                  pathOptions={{ color: dayStyle(route.day).color, weight: 3.5, opacity: faded ? 0.15 : 1, className: `route-day-${route.day}` }}
                  interactive={false}
                />
              </Fragment>
            );
          })}

          {data.airports.map((pin) => (
            <Marker
              key={pin.id}
              ref={keep(pin.id)}
              position={pin.position}
              icon={icons.airport}
              title={`Airport: ${pin.code}`}
              opacity={activeDay !== null ? 0.45 : 1}
            >
              <Popup className="tp-popup">
                <PopupCard eyebrow="Airport" title={pin.name ?? pin.code}>
                  <PopupRow icon={Plane}>
                    {pin.role === "origin" ? "Your flight departs from" : "Your flight arrives at"} {pin.code}
                  </PopupRow>
                </PopupCard>
              </Popup>
            </Marker>
          ))}

          {data.hotels.map((pin) => (
            <Marker key={pin.id} ref={keep(pin.id)} position={pin.position} icon={icons.hotel} title={`Hotel: ${pin.name}`} zIndexOffset={400}>
              <Popup className="tp-popup">
                <PopupCard eyebrow="Your stay" title={pin.name}>
                  {(pin.stars || pin.rating) && (
                    <PopupRow icon={Star}>
                      {[pin.stars ? `${pin.stars}-star` : null, pin.rating ? `${pin.rating}/10 guest rating` : null].filter(Boolean).join(" · ")}
                    </PopupRow>
                  )}
                  {pin.address && <PopupRow icon={BedDouble}>{pin.address}</PopupRow>}
                  <PopupRow icon={Ticket}>{formatINR(pin.costPerNight)} per night</PopupRow>
                </PopupCard>
              </Popup>
            </Marker>
          ))}

          {data.activities.map((pin) => {
            const category = categoryMeta(pin.category);
            const rating = describeRating(pin.rating);
            return (
              <Marker
                key={pin.id}
                ref={keep(pin.id)}
                position={pin.position}
                icon={icons.activities.get(pin.id)}
                title={`Day ${pin.day} · ${SLOT_LABELS[pin.slot]}: ${pin.name}`}
                opacity={dimmed(pin.day) ? 0.25 : 1}
                zIndexOffset={dimmed(pin.day) ? 0 : 800}
              >
                <Popup className="tp-popup">
                  <PopupCard
                    eyebrow={
                      <>
                        <Pin pinStyle={dayStyle(pin.day)} size="xs" />
                        Day {pin.day} · {SLOT_LABELS[pin.slot]}
                      </>
                    }
                    title={pin.name}
                  >
                    {category && <PopupRow icon={category.icon}>{category.label}</PopupRow>}
                    <PopupRow icon={Star}>Rating: {rating ? `${rating.label} (${rating.level} of 3)` : "not rated"}</PopupRow>
                    {rating?.heritage && <PopupRow icon={Landmark}>Heritage site</PopupRow>}
                    <PopupRow icon={Ticket}>Cost estimate: {pin.cost > 0 ? formatINR(pin.cost) : "free or not listed"}</PopupRow>
                  </PopupCard>
                </Popup>
              </Marker>
            );
          })}
        </MapContainer>
      </div>

      {unmapped}
    </section>
  );
}
