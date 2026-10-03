// ---------------------------------------------------------------------------
// Types mirroring the FastAPI Pydantic schemas.
// All UUIDs are strings (JSON-serialised by FastAPI).
// All datetimes are ISO 8601 UTC strings.
// ---------------------------------------------------------------------------

export type TripStatus = "pending" | "planning" | "completed" | "failed";
export type AgentStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface Trip {
  id: string;
  user_id: string;
  destination: string;
  start_date: string;   // "YYYY-MM-DD"
  end_date: string;
  budget: number;
  group_size: number;
  interests: string[] | null;
  status: TripStatus;
  created_at: string;
}

export interface AgentRun {
  id: string;
  trip_id: string;
  agent_name: string;
  status: string;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  duration_ms: number | null;
  turn: number;
  created_at: string;
}

// ── Itinerary structured_data shape (from ItineraryBuilder) ───────────────

export interface ActivitySlot {
  activity: string;
  cost: number;
  lat?: number | null;
  lng?: number | null;
  category?: string | null;   // Phase 18 — attached from the attraction search
  rating?: number | null;     // OpenTripMap popularity rate — see lib/places.ts
}

export interface HotelSlot {
  name: string;
  cost_per_night: number;
  stars?: number | null;
  rating?: number | null;     // guest review score, 0–10
  address?: string | null;
  lat?: number | null;
  lng?: number | null;
}

export interface Airport {
  code: string;               // IATA
  name?: string | null;
  lat?: number | null;
  lng?: number | null;
}

/** The outbound flight the itinerary's cost assumes — attached to the arrival day. */
export interface FlightInfo {
  airline?: string;
  flight_number?: string;
  departure?: string;
  arrival?: string;
  duration_mins?: number;
  price_inr?: number;       // for every traveller; both directions when the trip has a return date
  stops?: number;
  origin?: Airport | null;
  destination?: Airport | null;
}

export interface DaySchedule {
  day: number;
  date: string;
  morning: ActivitySlot | null;
  afternoon: ActivitySlot | null;
  evening: ActivitySlot | null;
  hotel: HotelSlot | null;
  flight: FlightInfo | null;
}

export interface StructuredItinerary {
  days: DaySchedule[];
  total_cost: number;
  currency: string;
  local_intelligence?: Record<string, unknown>;
}

export interface Itinerary {
  id: string;
  trip_id: string;
  content: string | null;
  structured_data: StructuredItinerary | null;
  total_cost: number | null;
  created_at: string;
}

// ── SSE event shapes ───────────────────────────────────────────────────────

export interface SSEConnectedEvent {
  event: "connected";
  trip_id: string;
  status: "listening";
}

export interface SSEAgentUpdateEvent {
  agent?: string;
  status?: AgentStatus;
  summary?: string;
  retryable?: boolean;          // a failed search (Phase 20): whether running it again could help
  event?: string;               // "planning_started" | "planning_complete" | "planning_failed" | "budget_conflict" | "builder_token"
  agents_done?: number;
  agents_total?: number;
  itinerary_id?: string | null;
  reason?: string;              // budget_conflict
  error?: string;               // planning_failed
  options?: BudgetConflictOption[];
  turn?: number;
  refinement_type?: string;     // planning_started, for a refinement or a retry
  retry?: string;               // planning_started: the search a retry repeats
  token?: string;               // builder_token (Phase 20): the next piece of the itinerary being written
  seq?: number;                 // builder_token: its place in the build's stream, from 0
}

export type ReplanChoice = "cheaper_flights" | "reduce_days" | "increase_budget";

export interface BudgetConflictOption {
  choice: ReplanChoice;
  description: string;
  estimated_saving: string;
}

/** Flights left too little of the budget for the rest of the trip — the ways out the user can pick from. */
export interface BudgetConflict {
  reason: string;
  options: BudgetConflictOption[];
}

export type SSEEvent = SSEConnectedEvent | SSEAgentUpdateEvent;

// ── Progress (from GET /trips/{id}/status) ────────────────────────────────

/** The run in flight, for a page that was loaded after its `planning_started` event had gone by. */
export interface CurrentRun {
  turn: number;
  refinement_type?: string;
  retry?: string;
}

export interface TripStatusResponse {
  status: TripStatus;
  trip_id: string;
  progress: {
    agents_done: number;
    agents_total: number;
    agents: Record<string, AgentStatus>;
    errors?: Record<string, string>;        // why each failed search failed (Phase 20)
    retryable?: Record<string, boolean>;    // whether running each failed search again could help
  };
  run?: CurrentRun | null;                  // null unless a run is in flight
  budget_conflict: BudgetConflict | null;   // set when the last run ended in one — survives a reload
  failure_reason: string | null;            // why the last run failed, when the planner can say
}

// ── Auth ─────────────────────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
}

export interface UserRead {
  id: string;
  email: string;
  created_at: string;
}

// ── Error envelope ────────────────────────────────────────────────────────
// Every error response carries `error`; `detail` is FastAPI's own field.

export interface ApiErrorBody {
  detail: unknown;
  error?: { code: string; message: string };
}
