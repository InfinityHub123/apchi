import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";

const STAGING = new Set(["created", "updated", "removed", "reset", "imported", "reverted", "rolled_back"]);
const NOT_STAGING = new Set(["pipeline", "admin", "audit"]);

/**
 * How many times the Candidate changed after `since`, from the trail: a Validation checked
 * the Candidate as it was when it started, so every staging act after that is something it
 * did not see. Null while reading.
 */
export function useChangedSince(since: string | undefined) {
  const read = useQuery({
    queryKey: ["authorship", "since", since],
    queryFn: () =>
      api
        .GET("/api/v1/audit", { params: { query: { since, outcome: "succeeded", limit: 500 } } })
        .then(unwrap),
    enabled: since !== undefined,
  });
  if (read.data === undefined) return null;
  return read.data.items.filter(
    (e) =>
      STAGING.has(e.action) &&
      // Apchi's own acts -- an Auto Rollback -- move the cluster, not the Candidate.
      e.actor !== "apchi" &&
      (!NOT_STAGING.has(e.category) || ["imported", "reset", "rolled_back"].includes(e.action)),
  ).length;
}
