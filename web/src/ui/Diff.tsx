import { useState } from "react";
import { isSecretName, MASK } from "../secrets";

type Json = unknown;

/**
 * What one staged change actually does, field by field.
 *
 * §21 requires the full Candidate diff before an Apply. The inventory -- which resources
 * changed -- is the table; this is the other half, because "events_kafka changed" does not
 * tell an Operator whether a broker moved or the whole topic list was replaced.
 *
 * Nested objects are flattened to dotted paths rather than rendered as a tree. A Trino
 * connector's configuration is a flat property bag in every way that matters, and a tree
 * makes the reader expand three levels to find that one string moved.
 */
export function Diff({ before, after }: { before: Json; after: Json }) {
  const rows = fields(before, after);

  if (rows.length === 0) {
    return <p className="diff__empty">No field changed. The resource was re-saved as it was.</p>;
  }

  return (
    <table className="diff">
      <caption className="sr-only">Every field this change adds, alters or removes</caption>
      <thead>
        <tr>
          <th scope="col">Field</th>
          <th scope="col">Before</th>
          <th scope="col">After</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.path}>
            <th scope="row" className="diff__field">
              {row.path}
            </th>
            <td className="diff__before">
              <Value value={row.before} name={row.path} absent={row.before === undefined} />
            </td>
            <td className="diff__after">
              <Value value={row.after} name={row.path} absent={row.after === undefined} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Value({
  value,
  name,
  absent,
}: {
  value: Json;
  name: string;
  absent: boolean;
}) {
  const [revealed, setRevealed] = useState(false);
  const secret = isSecretName(name);

  if (absent) {
    return (
      <span className="diff__absent">
        <span className="sr-only">not set</span>
        <span aria-hidden="true">—</span>
      </span>
    );
  }

  const text = render(value);

  if (secret && !revealed) {
    return (
      <button
        type="button"
        className="diff__reveal"
        onClick={() => setRevealed(true)}
        aria-label={`Reveal ${name}`}
      >
        <span aria-hidden="true">{MASK}</span>
        <span className="diff__revealhint">reveal</span>
      </button>
    );
  }

  return <span className="diff__value">{text}</span>;
}

type Row = { path: string; before: Json | undefined; after: Json | undefined };

/** Every path that differs between the two sides, in a stable order. */
function fields(before: Json, after: Json): Row[] {
  const a = flatten(before);
  const b = flatten(after);
  const paths = [...new Set([...a.keys(), ...b.keys()])].sort();

  return paths
    .map((path) => ({ path, before: a.get(path), after: b.get(path) }))
    .filter((row) => render(row.before) !== render(row.after));
}

function flatten(value: Json, prefix = "", into = new Map<string, Json>()): Map<string, Json> {
  if (value === null || value === undefined) return into;
  if (typeof value !== "object" || Array.isArray(value)) {
    if (prefix !== "") into.set(prefix, value);
    return into;
  }
  for (const [key, child] of Object.entries(value as Record<string, Json>)) {
    flatten(child, prefix === "" ? key : `${prefix}.${key}`, into);
  }
  return into;
}

function render(value: Json): string {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
