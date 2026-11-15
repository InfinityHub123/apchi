import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";

/**
 * Every read the shell needs, in one place.
 *
 * Apchi exposes no websocket and no push for cluster state, so the shell polls. The intervals
 * come from what each answer costs the cluster rather than from one default: `/review` reads
 * `queries_at_risk` live from the coordinator, so it refetches when an Operator looks at it
 * rather than on a timer nobody is watching.
 */

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => api.GET("/api/v1/health").then(unwrap),
    refetchInterval: 15_000,
  });
}

export function useReview() {
  return useQuery({
    queryKey: ["review"],
    queryFn: () => api.GET("/api/v1/review").then(unwrap),
    // The count is stale the moment it is read, and the contract says so in those words.
    refetchOnWindowFocus: true,
  });
}

export function useSnapshots() {
  return useQuery({
    queryKey: ["snapshots"],
    queryFn: () => api.GET("/api/v1/snapshots").then(unwrap),
  });
}

