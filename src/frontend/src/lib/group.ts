/**
 * A group trip's summary, as the itinerary carries it (Phase 25, src/ai/group.py `summary`).
 *
 * Read forgivingly, like everything else a saved itinerary holds: an itinerary planned before
 * Phase 25, or for travellers who were not told apart, has none — and then there is no panel.
 */

export interface GroupMemberSummary {
  name: string;
  interests: string[];
  stops: number;          // the plan's stops that answer one of this traveller's interests
  nothingFor: string[];   // interests of theirs that no place was found for
}

export interface GroupSummary {
  members: GroupMemberSummary[];
  balanced: boolean;      // in every two days, a stop for every traveller who said what they enjoy
}

function words(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];
}

export function readGroup(raw: unknown): GroupSummary | null {
  if (!raw || typeof raw !== "object") return null;
  const data = raw as Record<string, unknown>;
  if (!Array.isArray(data.members)) return null;

  const members = data.members.flatMap((entry): GroupMemberSummary[] => {
    if (!entry || typeof entry !== "object") return [];
    const member = entry as Record<string, unknown>;
    if (typeof member.name !== "string" || !member.name.trim()) return [];
    return [
      {
        name: member.name.trim(),
        interests: words(member.interests),
        stops: typeof member.stops === "number" && member.stops > 0 ? Math.round(member.stops) : 0,
        nothingFor: words(member.nothing_for),
      },
    ];
  });
  return members.length >= 2 ? { members, balanced: data.balanced === true } : null;
}

/**
 * One traveller's share of the total, split equally; null for a trip of one. The same sum an
 * itinerary carries as `per_person_cost` (pricing.per_person) — worked out here, so that a plan
 * saved before Phase 25 shows its share too. The PDF does the same (pdf/plan.py).
 */
export function perPersonCost(total: number, travellers: number | null): number | null {
  if (!travellers || travellers < 2 || !(total > 0)) return null;
  return total / travellers;
}
