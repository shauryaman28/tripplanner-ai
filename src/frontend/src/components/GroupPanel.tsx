"use client";

import { CircleCheck, Info, Users } from "lucide-react";

import { plural } from "@/lib/format";
import { readGroup } from "@/lib/group";

interface Props {
  /** `structured_data.group` — whatever is there; a trip whose travellers were not told apart shows no panel. */
  group: unknown;
  destination: string | null;
}

/**
 * Who a group trip is for, and how the plan turned out for each of them (Phase 25).
 *
 * The plan is balanced by code, not by trust: every two days hold a stop for each traveller, as far
 * as the places found allow. What was not found is said here — nobody is told "something for
 * everyone" about a plan that has nothing for one of them.
 */
export default function GroupPanel({ group, destination }: Props) {
  const summary = readGroup(group);
  if (!summary) return null;
  const where = destination ? ` in ${destination}` : "";

  return (
    <section aria-label="Who it is for" className="card p-5 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 className="flex items-center gap-2 font-display text-xl font-medium tracking-tight text-ink-900">
          <Users className="h-5 w-5 text-ink-400" aria-hidden />
          Who it is for
        </h2>
        <p
          data-balanced={summary.balanced}
          className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${
            summary.balanced ? "bg-good-soft text-good-ink" : "bg-warn-soft text-warn-ink"
          }`}
        >
          {summary.balanced ? <CircleCheck className="h-3.5 w-3.5" aria-hidden /> : <Info className="h-3.5 w-3.5" aria-hidden />}
          {summary.balanced ? "Something for everyone, every two days" : "Not everyone could be given a stop every two days"}
        </p>
      </div>

      <ul className="mt-4 grid gap-px overflow-hidden rounded-xl border border-ink-200/70 bg-ink-200/70 sm:grid-cols-2">
        {summary.members.map((member) => (
          <li key={member.name} data-member={member.name} className="bg-white px-4 py-3.5">
            <p className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="font-semibold text-ink-900">{member.name}</span>
              <span className="text-sm text-ink-600">{member.interests.length ? plural(member.stops, "stop") : "Along for the ride"}</span>
            </p>
            {member.interests.length > 0 && <p className="mt-0.5 text-xs capitalize text-ink-500">{member.interests.join(" · ")}</p>}
            {member.nothingFor.length > 0 && (
              <p className="mt-1.5 text-xs text-warn-ink">
                Nothing was found{where} for: {member.nothingFor.join(", ")}
              </p>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-ink-500">Each stop below says who it is for. The cost is shared equally.</p>
    </section>
  );
}
