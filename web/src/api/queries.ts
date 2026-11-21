import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, done, unwrap } from "./client";

/**
 * The reads every screen shares, and the cadence each one is allowed.
 *
 * Apchi exposes no push for cluster state, so the shell polls -- but only what is cheap.
 * `/cluster` is four MongoDB reads and is the one polled header. `/cluster/probe` and
 * `/cluster/drift` are requests to the coordinator, so they are read on demand and never on
 * a timer: a console left open in forty tabs must not be a load test on Trino.
 */

export function useCluster() {
  return useQuery({
    queryKey: ["cluster"],
    queryFn: () => api.GET("/api/v1/cluster").then(unwrap),
    refetchInterval: 10_000,
  });
}

/** One reading of the coordinator, taken when the console opens and when somebody asks. */
export function useProbe() {
  return useQuery({
    queryKey: ["probe"],
    queryFn: () =>
      api.GET("/api/v1/cluster/probe", { params: { query: { workers: true } } }).then(unwrap),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useReview() {
  return useQuery({
    queryKey: ["review"],
    queryFn: () => api.GET("/api/v1/review").then(unwrap),
    // `queries_at_risk` in here is a live reading of the coordinator, and stale once read.
    refetchOnWindowFocus: true,
  });
}

export function useApply(id: string | null | undefined) {
  return useQuery({
    queryKey: ["apply", id],
    queryFn: () =>
      api.GET("/api/v1/applies/{apply_id}", { params: { path: { apply_id: id! } } }).then(unwrap),
    enabled: id != null,
  });
}

/**
 * Anything that changes the Candidate invalidates everything derived from it. Coarse on
 * purpose: the Review, every Section listing and the shell's counts all read the one shared
 * Candidate, and a precise invalidation is a list somebody forgets to extend.
 */
export function useInvalidateCandidate() {
  const queries = useQueryClient();
  return () =>
    Promise.all([
      queries.invalidateQueries({ queryKey: ["review"] }),
      queries.invalidateQueries({ queryKey: ["section"] }),
      queries.invalidateQueries({ queryKey: ["authorship"] }),
    ]);
}

export function useReset() {
  const invalidate = useInvalidateCandidate();
  return useMutation({
    mutationFn: () => api.POST("/api/v1/candidate/reset").then(done),
    onSuccess: invalidate,
  });
}

export function useStartValidation() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: () => api.POST("/api/v1/validations").then(unwrap),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["validations"] }),
  });
}
