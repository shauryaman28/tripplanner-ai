/**
 * Typed API client for the TripPlanner FastAPI backend.
 *
 * All routes are prefixed with /api which Next.js rewrites to the backend.
 * The token lives in localStorage — simplest thing that survives a reload;
 * a production app would use an httpOnly cookie.
 */

import type {
  ApiErrorBody,
  GroupMember,
  Itinerary,
  ReplanChoice,
  SimilarTripsResponse,
  TokenResponse,
  Trip,
  TripSearchResponse,
  TripStatus,
  TripStatusResponse,
  UserRead,
} from "./types";

// ---------------------------------------------------------------------------
// Token store — simplest possible; replace with httpOnly cookie in production
// ---------------------------------------------------------------------------

let _token: string | null = null;

export function setToken(token: string): void {
  _token = token;
  if (typeof window !== "undefined") {
    localStorage.setItem("tp_token", token);
  }
}

export function getToken(): string | null {
  if (_token) return _token;
  if (typeof window !== "undefined") {
    _token = localStorage.getItem("tp_token");
  }
  return _token;
}

export function clearToken(): void {
  _token = null;
  if (typeof window !== "undefined") {
    localStorage.removeItem("tp_token");
    localStorage.removeItem("tp_email");
  }
}

/** The signed-in user's email, remembered at login for the header (the API has no "who am I" route). */
export function getEmail(): string | null {
  return typeof window !== "undefined" ? localStorage.getItem("tp_email") : null;
}

// ---------------------------------------------------------------------------
// Base fetch wrapper
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

/** A failed response as an ApiError, with the message from the error envelope when there is one. */
async function apiError(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body: ApiErrorBody = await res.json();
    detail = body.error?.message ?? (typeof body.detail === "string" ? body.detail : detail);
  } catch {
    // ignore parse error; use the status string
  }
  // An expired or revoked token: forget it, so the pages' auth guards send the user to /login.
  if (res.status === 401) clearToken();
  return new ApiError(res.status, detail);
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`/api${path}`, { ...options, headers });
  if (!res.ok) throw await apiError(res);

  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export async function register(email: string, password: string): Promise<UserRead> {
  return request<UserRead>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function login(email: string, password: string): Promise<string> {
  // OAuth2 form, not JSON
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!res.ok) {
    throw new ApiError(res.status, res.status === 401 ? "That email and password don't match." : "Could not sign in. Please try again.");
  }
  const data: TokenResponse = await res.json();
  setToken(data.access_token);
  localStorage.setItem("tp_email", email);
  return data.access_token;
}

// ---------------------------------------------------------------------------
// Trips
// ---------------------------------------------------------------------------

export async function listTrips(status?: TripStatus): Promise<Trip[]> {
  const qs = status ? `?status=${status}` : "";
  return request<Trip[]>(`/trips${qs}`);
}

export interface CreateTripPayload {
  destination: string;
  start_date: string;
  end_date: string;
  budget: number;
  group_size?: number;
  interests?: string[];
  group_members?: GroupMember[];   // Phase 25 — at least two, each with a name
}

export async function createTrip(payload: CreateTripPayload): Promise<Trip> {
  return request<Trip>("/trips", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface PlanTripPayload {
  raw_input?: string;
}

export async function getTrip(tripId: string): Promise<Trip> {
  return request<Trip>(`/trips/${tripId}`);
}

export type PlanResponse =
  | { status: "planning_started"; trip_id: string }
  | { status: "clarification_needed"; trip_id: string; question: string };

export async function planTrip(
  tripId: string,
  payload: PlanTripPayload = {},
): Promise<PlanResponse> {
  return request(`/trips/${tripId}/plan`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function clarifyTrip(
  tripId: string,
  answer: string,
): Promise<{ status: string }> {
  return request(`/trips/${tripId}/clarify`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

export async function refineTrip(
  tripId: string,
  message: string,
): Promise<{ status: string; turn: number; refinement_type: string }> {
  return request(`/trips/${tripId}/refine`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export async function replanTrip(
  tripId: string,
  choice: ReplanChoice,
): Promise<{ status: string; choice: ReplanChoice }> {
  return request(`/trips/${tripId}/replan`, {
    method: "POST",
    body: JSON.stringify({ choice }),
  });
}

export type RetryResponse =
  | { status: "retry_started"; trip_id: string; turn: number; refinement_type: string; agent: string }
  | PlanResponse;

/**
 * Try again what failed (Phase 20). With `agent` on a trip that has a plan, that one search runs
 * again and the plan keeps the rest; otherwise the whole trip is planned again.
 */
export async function retryTrip(tripId: string, agent?: string): Promise<RetryResponse> {
  return request(`/trips/${tripId}/retry`, {
    method: "POST",
    body: JSON.stringify(agent ? { agent } : {}),
  });
}

export async function getItinerary(tripId: string): Promise<Itinerary> {
  return request<Itinerary>(`/trips/${tripId}/itinerary`);
}

export async function getTripStatus(tripId: string): Promise<TripStatusResponse> {
  return request<TripStatusResponse>(`/trips/${tripId}/status`);
}

// ---------------------------------------------------------------------------
// Similar trips and search (Phase 23)
// ---------------------------------------------------------------------------

/** The traveller's other trips most like this one, most alike first — at most five. */
export async function getSimilarTrips(tripId: string): Promise<SimilarTripsResponse> {
  return request<SimilarTripsResponse>(`/trips/${tripId}/similar`);
}

/** The traveller's planned trips that match what was typed, best first. An empty list is an answer. */
export async function searchTrips(query: string): Promise<TripSearchResponse> {
  return request<TripSearchResponse>(`/trips/search?q=${encodeURIComponent(query)}`);
}

// ---------------------------------------------------------------------------
// PDF export (Phase 19)
// ---------------------------------------------------------------------------

export interface PdfFile {
  blob: Blob;
  /** the name the backend gave the file, e.g. "trip-goa-2027-12-10.pdf" */
  filename: string;
  /** the plan has places to show, but the map could not be drawn this time — the PDF came without it */
  mapMissing: boolean;
}

/** The latest itinerary as a PDF. The answer is the file itself, not JSON. */
export async function downloadTripPdf(tripId: string): Promise<PdfFile> {
  const token = getToken();
  const res = await fetch(`/api/trips/${tripId}/export/pdf`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw await apiError(res);

  const blob = await res.blob();
  // A 200 that is not a PDF (a proxy's error page) must not be saved as one.
  if (!blob.type.includes("pdf")) throw new ApiError(res.status, "The server did not send a PDF.");

  return {
    blob,
    filename: attachmentName(res.headers.get("Content-Disposition")) ?? "trip-itinerary.pdf",
    mapMissing: res.headers.get("X-Itinerary-Map") === "unavailable",
  };
}

/**
 * The file name a Content-Disposition header gives. `filename*` (RFC 6266) comes first: it carries
 * a name in any script, e.g. "trip-गोवा-2027-12-10.pdf" (Phase 20). Plain `filename` is ASCII only.
 */
export function attachmentName(header: string | null): string | null {
  const encoded = /filename\*\s*=\s*UTF-8''([^;\s]+)/i.exec(header ?? "");
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      // a malformed escape: the plain name below is still good
    }
  }
  return /filename\s*=\s*"?([^";]+)"?/i.exec(header ?? "")?.[1] ?? null;
}

export { ApiError };
