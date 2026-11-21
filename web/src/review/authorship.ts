import { useQueries, useQuery } from "@tanstack/react-query";
import { api, unwrap, type Review, type S, type SectionName } from "../api/client";
import { useCluster } from "../api/queries";
import { useIdentity } from "../auth/useAuth";

/**
 * Who staged each change, reconstructed from the audit trail.
 *
 * Authorship is kept out of the Candidate on purpose, so this is a reading of the trail and
 * the interface has to say when it does not know rather than guess. For each staged
 * resource the answer is the most recent act since the base Snapshot that could have put it
 * where it is:
 *
 * - an edit naming the resource: that person staged it;
 * - an act on the whole Section or the whole Candidate (a Section Revert, an import, a Full
 *   Rollback) that came later: the change arrived with that act, and saying "jo staged
 *   this" would credit jo with something a revert did;
 * - nothing at all: not known. An import before the trail's retention window, or a
 *   Candidate staged before the base Snapshot moved, lands here.
 */
export type Author =
  | { kind: "edit"; actor: string; via: string; at: string; mine: boolean }
  | { kind: "bulk"; actor: string; at: string; action: string; mine: boolean }
  | { kind: "unknown" };

export type Authorship = Map<string, Author>;

export const key = (section: SectionName, resource: string) => `${section}\u0000${resource}`;

const BULK = new Set(["reverted", "imported", "rolled_back", "reset"]);

async function allSince(query: { category: S["AuditEvent"]["category"]; since?: string }) {
  const events: S["AuditEvent"][] = [];
  let before: string | undefined;
  // Five pages of 500 is far beyond any Candidate a person reviews; past it the remaining
  // changes read as "not known" rather than this loop reading the trail without end.
  for (let page = 0; page < 5; page++) {
    const result = await api
      .GET("/api/v1/audit", {
        params: { query: { ...query, outcome: "succeeded", limit: 500, before } },
      })
      .then(unwrap);
    events.push(...result.items);
    if (result.next == null) break;
    before = result.next;
  }
  return events;
}

/** When the Candidate's base Snapshot was committed: where its staged history begins. */
function useBaseTime(base: number | null | undefined) {
  const cluster = useCluster();
  const sameAsLive = base != null && cluster.data?.snapshot === base;
  const fetched = useQuery({
    queryKey: ["snapshot-time", base],
    queryFn: () =>
      api
        .GET("/api/v1/snapshots/{number}", { params: { path: { number: base! } } })
        .then(unwrap)
        .then((s) => s.created_at),
    enabled: base != null && cluster.data !== undefined && !sameAsLive,
    staleTime: Infinity,
  });
  if (base == null) return { ready: true, since: undefined };
  if (sameAsLive) return { ready: true, since: cluster.data?.snapshot_at ?? undefined };
  return { ready: fetched.data !== undefined, since: fetched.data };
}

/**
 * Null until every read it depends on has answered; a partial answer would count somebody's
 * change as "not known" for a moment and then change its mind.
 */
export function useAuthorship(review: Review | undefined): Authorship | null {
  const me = useIdentity().data?.subject;
  const base = useBaseTime(review?.base_snapshot);
  const changed = (review?.sections ?? []).filter((s) => (s.changes?.length ?? 0) > 0);
  const enabled = review !== undefined && base.ready;

  const reads = useQueries({
    queries: [
      {
        queryKey: ["authorship", "pipeline", base.since],
        queryFn: () => allSince({ category: "pipeline", since: base.since }),
        enabled,
      },
      ...changed.map((s) => ({
        queryKey: ["authorship", s.section, base.since],
        queryFn: () => allSince({ category: s.section, since: base.since }),
        enabled,
      })),
    ],
  });

  if (!enabled || reads.some((r) => r.data === undefined)) return null;

  // `apchi` acting is an Auto Rollback or recovery at start-up, which put the *cluster*
  // back and never touched the Candidate.
  const pipeline = (reads[0]?.data ?? []).filter((e) => BULK.has(e.action) && e.actor !== "apchi");
  const out: Authorship = new Map();

  changed.forEach((s, i) => {
    const events = reads[i + 1]?.data ?? [];
    for (const change of s.changes ?? []) {
      const candidates = [
        ...events.filter(
          (e) => e.resource === change.resource || (e.resource == null && BULK.has(e.action)),
        ),
        ...pipeline,
      ];
      // Newest first by instant, then by id -- the trail's own sort key within one instant.
      candidates.sort((a, b) => (a.at === b.at ? (a.id < b.id ? 1 : -1) : a.at < b.at ? 1 : -1));
      const latest = candidates[0];
      out.set(
        key(s.section, change.resource),
        latest === undefined
          ? { kind: "unknown" }
          : latest.resource === change.resource
            ? { kind: "edit", actor: latest.actor, via: latest.via, at: latest.at, mine: latest.actor === me }
            : { kind: "bulk", actor: latest.actor, at: latest.at, action: latest.action, mine: latest.actor === me },
      );
    }
  });
  return out;
}

/** How many staged changes somebody other than the caller is known to have staged. */
export function othersCount(authorship: Authorship | null): number | null {
  if (authorship === null) return null;
  let n = 0;
  for (const a of authorship.values()) if (a.kind !== "unknown" && !a.mine) n++;
  return n;
}

const BULK_LABEL: Record<string, string> = {
  reverted: "a Section Revert",
  imported: "an import",
  rolled_back: "a Full Rollback",
  reset: "a Reset",
};

/** "jo", "you", "a Section Revert by mira", "not known". */
export function authorLabel(a: Author | undefined): string {
  if (a === undefined) return "…";
  if (a.kind === "unknown") return "not known";
  const who = a.mine ? "you" : a.actor;
  return a.kind === "edit" ? who : `${BULK_LABEL[a.action] ?? a.action} by ${who}`;
}
