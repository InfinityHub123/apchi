import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import type { components } from "./schema";

export type Snapshot = components["schemas"]["Snapshot"];
export type AuditEvent = components["schemas"]["AuditEvent"];
export type ClientCertificate = components["schemas"]["ClientCertificate"];

/** The newest Snapshots, newest first. The first is the committed configuration. */
export function useSnapshots(limit = 5) {
  return useQuery({
    queryKey: ["snapshots", limit],
    queryFn: () =>
      api.GET("/api/v1/snapshots", { params: { query: { limit } } }).then(unwrap),
  });
}

/**
 * Certificates Apchi already calls expiring, rather than a date this UI compares itself.
 *
 * The window is `certificates.expiringWithinDays`, an Apchi setting, and the status is
 * recomputed on every read because the certificate is the truth -- a copy of its expiry
 * cached anywhere is a second truth that can be wrong.
 */
export function useExpiringCertificates() {
  return useQuery({
    queryKey: ["certificates", "expiring"],
    queryFn: () =>
      api
        .GET("/api/v1/certificates", { params: { query: { status: "expiring" } } })
        .then(unwrap),
  });
}

/**
 * Who staged what is in the audit trail and nowhere else.
 *
 * The Candidate stores no per-resource authorship, deliberately: `app/pipeline/candidate.py`
 * says "the audit of who changed what is the only record of whose work an Apply is
 * shipping". So §21's "and by whom" is answered by reading the trail rather than by adding
 * a field, and this is following that design rather than working around it.
 */
export function useRecentActivity(limit = 50) {
  return useQuery({
    queryKey: ["audit", "recent", limit],
    queryFn: () => api.GET("/api/v1/audit", { params: { query: { limit } } }).then(unwrap),
  });
}

/**
 * Who last touched each resource, as far as the audit can say.
 *
 * Deliberately conservative. It attributes a resource only when the trail names it
 * unambiguously, and returns nothing for one it cannot -- an interface that guesses who
 * made a change is worse than one that says it does not know, because the guess is the
 * thing somebody will quote in an incident review.
 */
export function attribute(events: AuditEvent[] | undefined): Map<string, string> {
  const by = new Map<string, string>();
  if (events === undefined) return by;

  // Newest first, so the first mention of a resource is the most recent one.
  for (const event of events) {
    if (event.resource === null || event.resource === undefined) continue;
    if (event.outcome !== "succeeded") continue;
    const key = `${event.category}:${event.resource}`;
    if (!by.has(key)) by.set(key, event.actor);
  }
  return by;
}
