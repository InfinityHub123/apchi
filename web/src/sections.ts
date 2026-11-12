import type { components } from "./api/schema";

export type SectionName = components["schemas"]["SectionDiff"]["section"];

/**
 * The six Sections, and whether applying a change to one forces a coordinator rollout.
 *
 * `restarts` is a fact about Trino, not about Apchi: catalogs are applied by SQL DDL against
 * the running coordinator and permissions and client certificates are file applies Trino
 * re-reads on its own timer, while certificate mapping, resource groups and event listeners
 * are read only at start-up (§7.1-§7.3).
 *
 * It lives here because the API does not expose it per Section. `GET /api/v1/review` answers
 * `cost.restarts_coordinator` for the Candidate as a whole, which is enough to warn before an
 * Apply but not enough for §21's requirement to show *which* changes force a restart. Until
 * the contract carries it, this table is the frontend's copy of a backend fact -- a second
 * source of truth, and the reason it is one table with one comment rather than a flag
 * scattered across six screens. See the API gap recorded on the frontend branch.
 */
export const SECTIONS: ReadonlyArray<{
  key: SectionName;
  label: string;
  restarts: boolean;
}> = [
  { key: "catalogs", label: "Catalogs", restarts: false },
  { key: "client_certificates", label: "Client certificates", restarts: false },
  { key: "certificate_mapping", label: "Certificate mapping", restarts: true },
  { key: "permissions", label: "Permissions", restarts: false },
  { key: "resource_groups", label: "Resource groups", restarts: true },
  { key: "event_listeners", label: "Event listeners", restarts: true },
];

const BY_KEY = new Map(SECTIONS.map((s) => [s.key, s]));

export function sectionLabel(key: SectionName): string {
  return BY_KEY.get(key)?.label ?? key;
}

export function sectionRestarts(key: SectionName): boolean {
  return BY_KEY.get(key)?.restarts ?? false;
}
