/**
 * Phase 39 — where the plan's flight and hotel are booked.
 *
 * The planner puts a link on each (src/ai/booking.py): a Google Flights search for the route, the days and the
 * travellers; a Booking.com search for the hotel, the nights and the guests. The page opens one only if it is
 * what the planner makes — https, to one of those two searches — and shows the button greyed out otherwise: an
 * itinerary made before this phase has no links, and a link from anywhere else is not followed.
 */

const BOOKING_SITES = ["https://www.google.com/travel/flights?", "https://www.booking.com/searchresults.html?"];

/** The link, if it is one of the two the planner makes; null otherwise. */
export function bookingUrl(url: string | null | undefined): string | null {
  return typeof url === "string" && BOOKING_SITES.some((site) => url.startsWith(site)) ? url : null;
}
