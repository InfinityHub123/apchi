import type { Review } from "../api/client";

/**
 * How many staged changes there are and how many of them ride a Rollout.
 *
 * A change rides the Rollout when its Section is in `cost.rollout_sections` -- the API's
 * answer, which also covers a Section in the Rollout because of drift with nothing staged
 * in it. That Section adds no change here, which is why `rollout` can be 0 while the Apply
 * still restarts the coordinator.
 */
export function candidateCounts(review: Review) {
  const rollout = new Set(review.cost.rollout_sections ?? []);
  let total = 0;
  let inRollout = 0;
  for (const s of review.sections) {
    const n = s.changes?.length ?? 0;
    total += n;
    if (rollout.has(s.section)) inRollout += n;
  }
  return { total, rollout: inRollout, noRestart: total - inRollout };
}
