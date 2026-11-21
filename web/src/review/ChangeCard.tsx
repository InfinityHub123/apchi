import { useState } from "react";
import type { S, SectionName } from "../api/client";
import { useResetResource, useSchemas } from "../api/queries";
import { resourceLabel as labelOf, section } from "../sections";
import { clock } from "../ui/format";
import { ChangePill, CostPill } from "../ui/Pill";
import { ErrorNote } from "../ui/States";
import { authorLabel, type Author } from "./authorship";
import { describeSelector, fieldChanges, secretTest, selectorMoves, show } from "./diff";

/**
 * One staged change: what it is, who staged it, what it costs, which fields moved, and the
 * per-row Reset that puts just this one back.
 */
export function ChangeCard({
  sectionName,
  change,
  author,
  rollout,
  base,
  locked,
}: {
  sectionName: SectionName;
  change: S["ResourceChange"];
  author: Author | undefined;
  rollout: boolean;
  base: number | null;
  locked: boolean;
}) {
  const meta = section(sectionName);
  const reset = useResetResource();
  const resourceLabel = labelOf(change.resource);
  const baseName = base == null ? "the empty configuration" : `Snapshot ${base}`;

  return (
    <section className="card" aria-label={`${meta.noun} ${resourceLabel}`}>
      <div className="chd">
        <ChangePill change={change.change} staged={false} />
        <span className="ct">
          {meta.noun} · <span className="mono">{resourceLabel}</span>
        </span>
        <span className="sm muted">
          {author === undefined
            ? "reading the audit…"
            : author.kind === "unknown"
              ? "author not known"
              : `staged by ${authorLabel(author)} · ${clock(author.at)}`}
        </span>
        <span className="sp" />
        <CostPill rollout={rollout} />
        <button
          type="button"
          className="btn btn--sm"
          disabled={locked || reset.isPending}
          title={`Puts this one resource back as ${baseName} has it. Every other staged change stays, including other people's.`}
          onClick={() => reset.mutate({ section: sectionName, resource: change.resource })}
        >
          {reset.isPending ? "Resetting…" : "Reset"}
        </button>
      </div>

      <ChangeBody sectionName={sectionName} change={change} />

      {change.change === "removed" && sectionName === "catalogs" && (
        <div className="note note--red" style={{ marginTop: 15 }}>
          Dropped from the running cluster at the next Apply. Queries that use{" "}
          <span className="mono">{change.resource}</span> will fail from then on.
        </div>
      )}
      {rollout && change.change !== "removed" && (
        <div className="note">
          {meta.label} {sectionName === "certificate_mapping" ? "is" : "are"} read once, when the coordinator
          starts. Applying this restarts the coordinator — <b>running queries will be terminated</b>.
        </div>
      )}
      {author?.kind === "unknown" && (
        <div className="note note--blue">
          <b>Who staged this is not known.</b> The audit has no event naming{" "}
          <span className="mono">{resourceLabel}</span> since {baseName} — it may have come in with
          an import. Apchi does not guess.
        </div>
      )}
      {author?.kind === "bulk" && (
        <div className="note note--blue">
          This arrived with {authorLabel(author)}, not an edit to{" "}
          <span className="mono">{resourceLabel}</span> itself.
        </div>
      )}
      {reset.isError && (
        <div style={{ padding: "0 15px 15px" }}>
          <ErrorNote error={reset.error} />
        </div>
      )}
    </section>
  );
}

function ChangeBody({ sectionName, change }: { sectionName: SectionName; change: S["ResourceChange"] }) {
  const schemas = useSchemas();

  if (change.resource === "#selectors") {
    const moves = selectorMoves(change.before, change.after);
    return (
      <div style={{ padding: "6px 0" }}>
        {moves.map((m, i) => (
          <div className="cr" key={i}>
            <span className="muted">
              Selector → <span className="mono">{m.selector.group}</span>
            </span>
            <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {m.from !== null && <span className="old">position {m.from + 1}</span>}
              {m.from !== null && m.to !== null && <span className="muted" aria-hidden="true">→</span>}
              {m.to !== null && <span className="new">position {m.to + 1}</span>}
              {m.to === null && <span className="sm muted">removed</span>}
              {m.from === null && <span className="sm muted">added</span>}
              <span className="sm muted mono">{describeSelector(m.selector)}</span>
            </span>
          </div>
        ))}
      </div>
    );
  }

  if (change.change === "removed") return null;

  const body = change.after ?? change.before;
  const isSecret = secretTest(sectionName, body, schemas);
  const rows = fieldChanges(change.change === "added" ? null : change.before, change.after, isSecret);

  return (
    <div style={{ padding: "6px 0" }}>
      {rows.map((r) => (
        <div className="cr" key={r.path}>
          <span className="muted mono" style={{ fontSize: 11.5, wordBreak: "break-all" }}>
            {r.path.replace(/^properties\./, "")}
          </span>
          <span style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", minWidth: 0 }}>
            {change.change === "added" ? (
              <Value value={r.after} secret={r.secret} />
            ) : (
              <>
                <Value value={r.before} secret={r.secret} kind="old" />
                <span className="muted" aria-hidden="true">
                  →
                </span>
                <Value value={r.after} secret={r.secret} kind="new" />
              </>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * A value in a diff. Secrets are masked until shown, so they do not appear by accident on a
 * shared screen -- a precaution, not a control: the API returns them in full. Long values
 * (a PEM certificate) are cut, because a diff is for seeing *that* something moved.
 */
export function Value({
  value,
  secret,
  kind,
}: {
  value: unknown;
  secret: boolean;
  kind?: "old" | "new";
}) {
  const [shown, setShown] = useState(false);
  const text = show(value);
  const masked = secret && value !== undefined && value !== null && !shown;
  const long = text.length > 160 && !shown;
  const display = masked ? "••••••••" : long ? `${text.slice(0, 120)}…` : text;
  const cls = kind ?? "";
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", minWidth: 0 }}>
      <span className={cls || "mono"} style={cls ? undefined : { fontSize: 12, wordBreak: "break-all" }}>
        {display}
      </span>
      {(masked || long) && (
        <button type="button" className="btn btn--sm" onClick={() => setShown(true)}>
          Show
        </button>
      )}
    </span>
  );
}
