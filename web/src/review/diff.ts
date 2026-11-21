import type { S, SectionName } from "../api/client";

/**
 * Which fields of one resource moved, as rows a person can read.
 *
 * A resource is whatever shape its Section stores, so this flattens both sides to dotted
 * paths and compares leaves. A list compares element by element, which is right for every
 * list a Section stores except the selector list, whose order is its meaning -- that one
 * has its own reading in `selectorMoves`.
 */
export type FieldChange = { path: string; before: unknown; after: unknown; secret: boolean };

type Json = unknown;

function flatten(value: Json, prefix: string, out: Map<string, Json>) {
  if (value !== null && typeof value === "object") {
    const entries = Array.isArray(value)
      ? value.map((v, i) => [String(i), v] as const)
      : Object.entries(value as Record<string, Json>);
    if (entries.length === 0) out.set(prefix, value);
    for (const [k, v] of entries) flatten(v, prefix === "" ? k : `${prefix}.${k}`, out);
  } else {
    out.set(prefix, value);
  }
}

export function fieldChanges(
  before: Json,
  after: Json,
  isSecret: (path: string) => boolean,
): FieldChange[] {
  const a = new Map<string, Json>();
  const b = new Map<string, Json>();
  if (before != null) flatten(before, "", a);
  if (after != null) flatten(after, "", b);
  const paths = [...new Set([...a.keys(), ...b.keys()])].sort();
  return paths
    .filter((p) => JSON.stringify(a.get(p)) !== JSON.stringify(b.get(p)))
    .map((p) => ({ path: p, before: a.get(p), after: b.get(p), secret: isSecret(p) }));
}

/**
 * Which properties of a resource are secrets, from the schemas the write endpoints enforce.
 *
 * For a Catalog on a connector Apchi has no schema for, nothing says which property holds a
 * password, so the property's name is the only evidence. Erring towards hiding is right
 * here and nowhere else: masking is a screen-share precaution, not access control, and the
 * cost of a wrongly masked value is one click on "Show".
 */
const LOOKS_SECRET = /password|secret|token|credential|private|passphrase|api[-_.]?key/i;

export function secretTest(
  section: SectionName,
  body: Json,
  schemas: { connectors?: S["PropertyRules"][]; listeners?: S["PropertyRules"][] },
): (path: string) => boolean {
  const record = (body ?? {}) as Record<string, unknown>;
  if (section === "client_certificates") return (p) => p === "private_key";
  if (section === "catalogs" || section === "event_listeners") {
    const kind = String(section === "catalogs" ? record.connector : record.type);
    const rules = (section === "catalogs" ? schemas.connectors : schemas.listeners)?.find(
      (r) => r.name === kind,
    );
    return (p) => {
      if (!p.startsWith("properties.")) return false;
      const name = p.slice("properties.".length);
      return rules !== undefined ? rules.secrets.includes(name) : LOOKS_SECRET.test(name);
    };
  }
  return () => false;
}

/**
 * The selector list, read as moves: where each selector was and where it is now.
 *
 * Selectors are checked top to bottom and the first match wins, so a selector moving up two
 * places changes which group a query lands in even though no selector's content changed.
 * Identity is the selector's own content -- there is no other.
 */
export type SelectorMove = {
  selector: S["Selector"];
  from: number | null;
  to: number | null;
};

export function selectorMoves(before: Json, after: Json): SelectorMove[] {
  const a = (Array.isArray(before) ? before : []) as S["Selector"][];
  const b = (Array.isArray(after) ? after : []) as S["Selector"][];
  const id = (s: S["Selector"]) => JSON.stringify(s);
  const used = new Set<number>();
  const moves: SelectorMove[] = [];
  b.forEach((s, to) => {
    const from = a.findIndex((x, i) => !used.has(i) && id(x) === id(s));
    if (from >= 0) used.add(from);
    if (from !== to) moves.push({ selector: s, from: from >= 0 ? from : null, to });
  });
  a.forEach((s, from) => {
    if (!used.has(from)) moves.push({ selector: s, from, to: null });
  });
  return moves.filter((m) => !(m.from !== null && m.to !== null && m.from === m.to));
}

/** A selector's conditions, as "user ~ ^svc_.* · queryType = INSERT". */
export function describeSelector(s: S["Selector"]): string {
  const parts: string[] = [];
  const re: Array<[keyof S["Selector"], string]> = [
    ["user", "user"],
    ["original_user", "originalUser"],
    ["authenticated_user", "authenticatedUser"],
    ["user_group", "userGroup"],
    ["source", "source"],
    ["query_text", "queryText"],
  ];
  for (const [k, label] of re) if (s[k] != null) parts.push(`${label} ~ ${String(s[k])}`);
  if (s.query_type != null) parts.push(`queryType = ${s.query_type}`);
  if (s.client_tags != null && s.client_tags.length > 0) parts.push(`clientTags ⊇ ${s.client_tags.join(", ")}`);
  return parts.length === 0 ? "No conditions — catches everything left" : parts.join(" · ");
}

export function show(value: unknown): string {
  if (value === undefined) return "absent";
  if (value === null) return "none";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
