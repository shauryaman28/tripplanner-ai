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
}

export interface HotelSlot {
  name: string;
  cost_per_night: number;
}

export interface DaySchedule {
  day: number;
  date: string;
  morning: ActivitySlot | null;
  afternoon: ActivitySlot | null;
  evening: ActivitySlot | null;
  hotel: HotelSlot | null;
  flight: Record<string, unknown> | null;
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
  event?: string;               // "planning_started" | "planning_complete" | etc.
  agents_done?: number;
  agents_total?: number;
  itinerary_id?: string | null;
  reason?: string;
  options?: BudgetConflictOption[];
  turn?: number;
}

export interface BudgetConflictOption {
  choice: string;
  description: string;
  estimated_saving: string;
}

export type SSEEvent = SSEConnectedEvent | SSEAgentUpdateEvent;

// ── Progress (from GET /trips/{id}/status) ────────────────────────────────

export interface TripStatusResponse {
  status: TripStatus;
  trip_id: string;
  progress: {
    agents_done: number;
    agents_total: number;
    agents: Record<string, AgentStatus>;
  };
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

export interface ApiError {
  detail: string | { code: string; message: string };
}
