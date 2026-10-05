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

/** What the DestinationIntelligenceAgent knew about the place (Phase 22) — a model's knowledge, not a search result. */
export interface LocalIntelligence {
  local_transport: string | null;
  cultural_norms: string[];
  tourist_traps: string[];
  best_times: Record<string, string>;   // place → when to go, and why
  safety_tips: string[];
}

export interface StructuredItinerary {
  days: DaySchedule[];
  total_cost: number;
  currency: string;
  local_intelligence?: LocalIntelligence | null;   // absent on older itineraries, null when the agent had nothing
}

export interface Itinerary {
  id: string;
  trip_id: string;
  content: string | null;
  structured_data: StructuredItinerary | null;
  total_cost: number | null;
  created_at: string;
}

// ── Similar trips and search (Phase 23) ───────────────────────────────────

/** One of the traveller's trips found by similarity: enough to show it as a card, and how close it came. */
export interface TripMatch {
  trip: Trip;
  itinerary_id: string;
  total_cost: number | null;
  highlight: string | null;   // one place to name the trip by: its most popular stop
  similarity: number;         // cosine similarity of the two embeddings; 1 is the same
}

export interface SimilarTripsResponse {
  status: "ready" | "pending"; // pending: this trip's own embedding is still being made
  results: TripMatch[];
}

export interface TripSearchResponse {
  query: string;
  results: TripMatch[];
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
  estimate?: ConflictEstimate | null;  // budget_conflict (Phase 21): the trip as asked, priced
  token?: string;               // builder_token (Phase 20): the next piece of the itinerary being written
  seq?: number;                 // builder_token: its place in the build's stream, from 0
}

export type ReplanChoice = "cheaper_flights" | "reduce_days" | "increase_budget" | "cheaper_hotel" | "off_peak";

export interface BudgetConflictOption {
  choice: ReplanChoice;
  description: string;
  estimated_saving: string;
  // Phase 21 — the trip with this change, priced at typical prices (whole rupees, to the nearest ₹100)
  total?: number | null;
  saving?: number | null;          // against the trip as asked
  flight_saving?: number | null;   // off_peak: what the flights would cost less
  fits?: boolean;                  // the total is within the budget
  days?: number;                   // reduce_days: the length offered
  start_date?: string;             // off_peak: the dates offered
  end_date?: string;
  budget?: number;                 // increase_budget: the budget offered
}

/** The trip as asked, priced at typical prices — what the options are measured against (Phase 21). */
export interface ConflictEstimate {
  total: number;
  total_min: number;   // where the cost will likely land once booked
  total_max: number;
  stay: string;        // "a 4-star hotel"
  month: number;       // 1–12
  season: "peak" | "shoulder" | "off-peak";
  about: string;       // "December is peak season in Goa: prices run about 40% above the off-season."
  budget: number;
}

/** Flights left too little of the budget for the rest of the trip — the ways out the user can pick from. */
export interface BudgetConflict {
  reason: string;
  options: BudgetConflictOption[];
  estimate?: ConflictEstimate | null;
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
