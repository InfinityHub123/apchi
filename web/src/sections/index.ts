import type { SectionName } from "../api/client";
import type { IconName } from "../ui/Icon";

/**
 * The five Sections as the console names and routes them.
 *
 * Presentation only. Whether a Section's change costs a Rollout is deliberately not here: it
 * comes from `Review.cost.rollout_sections`, because a Section is also in the Rollout when
 * the cluster drifted from what Apchi would write -- a fact no table in a browser can know.
 */
export const SECTIONS: ReadonlyArray<{
  key: SectionName;
  label: string;
  /** Short form, for the Section Revert tabs and other tight places. */
  short: string;
  /** What one resource is called, for "Catalog · finance_pg". */
  noun: string;
  path: string;
  icon: IconName;
  /** The path that writes it, for the revert route. */
  api: string;
}> = [
  { key: "catalogs", label: "Catalogs", short: "Catalogs", noun: "Catalog", path: "/catalogs", icon: "catalogs", api: "catalogs" },
  { key: "client_certificates", label: "Client certificates", short: "Certificates", noun: "Client certificate", path: "/certificates", icon: "key", api: "certificates" },
  { key: "certificate_mapping", label: "Certificate mapping", short: "Mapping", noun: "Certificate mapping", path: "/certificate-mapping", icon: "mapping", api: "certificate-mapping" },
  { key: "permissions", label: "Permissions", short: "Permissions", noun: "Permissions", path: "/permissions", icon: "shield", api: "permissions" },
  { key: "resource_groups", label: "Resource groups", short: "Resource groups", noun: "Resource groups", path: "/resource-groups", icon: "groups", api: "resource-groups" },
];

const BY_KEY = new Map(SECTIONS.map((s) => [s.key, s]));

export function section(key: SectionName) {
  const found = BY_KEY.get(key);
  if (found === undefined) throw new Error(`unknown Section ${key}`);
  return found;
}

/** A Section's label, or the key itself for anything that is not a Section. */
export const sectionLabel = (key: string) => BY_KEY.get(key as SectionName)?.label ?? key;

/**
 * A resource's name as a person reads it. Resource Groups keeps its selector list and its
 * settings as two resources whose keys start with `#` so they cannot collide with a group
 * path; on screen they are just "selectors" and "settings".
 */
export const resourceLabel = (resource: string) => (resource.startsWith("#") ? resource.slice(1) : resource);
