/**
 * Typed API client for the TripPlanner FastAPI backend.
 *
 * All routes are prefixed with /api which Next.js rewrites to the backend.
 * The token is stored in memory (module-level singleton) — for a production
 * app you would use a cookie or a state-management library.
 */

import type {
  Itinerary,
  TokenResponse,
  Trip,
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
  }
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

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // ignore parse error; use the status string
    }
    throw new ApiError(res.status, detail);
  }

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
  if (!res.ok) throw new ApiError(res.status, "Invalid credentials");
  const data: TokenResponse = await res.json();
  setToken(data.access_token);
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

export async function planTrip(
  tripId: string,
  payload: PlanTripPayload = {},
): Promise<{ status: string; trip_id: string }> {
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

export async function getItinerary(tripId: string): Promise<Itinerary> {
  return request<Itinerary>(`/trips/${tripId}/itinerary`);
}

export async function getTripStatus(tripId: string): Promise<TripStatusResponse> {
  return request<TripStatusResponse>(`/trips/${tripId}/status`);
}

export { ApiError };
