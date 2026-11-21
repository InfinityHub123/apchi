import { useQuery } from "@tanstack/react-query";
import { api, unwrap, type SectionName } from "../api/client";
import { useCluster, useReview } from "../api/queries";
import { key, useAuthorship } from "../review/authorship";

/**
 * What a Section screen needs beside its own listing: each resource's staged change, its
 * author, whether the Section rides the Rollout, and whether editing is allowed right now.
 */
export function useSectionState(name: SectionName) {
  const review = useReview();
  const cluster = useCluster();
  const authorship = useAuthorship(review.data);
  const diff = review.data?.sections.find((s) => s.section === name);
  const changes = new Map((diff?.changes ?? []).map((c) => [c.resource, c]));
  const last = cluster.data?.last_apply;
  const frozen = last != null && last.finished_at == null && last.stage !== "incident";
  return {
    changes,
    author: (resource: string) => authorship?.get(key(name, resource)),
    rollout: (review.data?.cost.rollout_sections ?? []).includes(name),
    base: review.data?.base_snapshot ?? null,
    live: cluster.data?.snapshot ?? null,
    locked: (cluster.data?.maintenance.engaged ?? false) || frozen,
  };
}

/** One Section as a committed Snapshot holds it, for the "Live" view. */
export function useSnapshotSection(snapshot: number | null, name: SectionName, enabled: boolean) {
  return useQuery({
    queryKey: ["snapshot", snapshot],
    queryFn: () => api.GET("/api/v1/snapshots/{number}", { params: { path: { number: snapshot! } } }).then(unwrap),
    enabled: enabled && snapshot !== null,
    staleTime: Infinity,
    select: (s) => (s.sections as Record<string, Record<string, unknown>>)[name] ?? {},
  });
}
