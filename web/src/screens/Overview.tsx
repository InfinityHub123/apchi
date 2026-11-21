import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { useCertificates, useCluster, useProbe, useReview, useSchemas, useSnapshots } from "../api/queries";
import { authorLabel, key, useAuthorship } from "../review/authorship";
import { candidateCounts } from "../review/counts";
import { fieldChanges, secretTest } from "../review/diff";
import { Value } from "../review/ChangeCard";
import { resourceLabel, sectionLabel } from "../sections";
import type { S } from "../api/client";
import { clock, date, dateTime, dayTime, plural } from "../ui/format";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, PageHead } from "../ui/States";

const MARK = { added: ["+", "var(--green)"], changed: ["~", "var(--accent)"], removed: ["−", "var(--red)"] } as const;

/** The home screen: what is live, what is staged, and what needs somebody's attention. */
export function OverviewScreen() {
  return (
    <div className="page">
      <PageHead
        title="Overview"
        sub="Nothing staged below has reached the cluster. An Apply is the only thing that changes it."
      />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
        <CommittedTile />
        <CoordinatorTile />
        <CandidateTile />
        <ExpiringTile />
      </div>
      <div className="row">
        <StagedTable />
        <div style={{ flex: "1 1 340px", minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          <RecentSnapshots />
          <StateCard />
        </div>
      </div>
    </div>
  );
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="card cp" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="lab">{label}</span>
      {children}
    </section>
  );
}

function CommittedTile() {
  const c = useCluster().data;
  return (
    <Tile label="Committed configuration">
      <span style={{ fontSize: 20, fontWeight: 600 }} className="mono">
        {c === undefined ? "…" : c.snapshot == null ? "None yet" : `Snapshot ${c.snapshot}`}
      </span>
      <span className="sm muted">
        {c?.snapshot_at != null
          ? `${dateTime(c.snapshot_at)} · Verification passed at that Apply — history, not a reading of now`
          : "Nothing has been applied to this cluster yet."}
      </span>
    </Tile>
  );
}

function CoordinatorTile() {
  const probe = useProbe();
  const p = probe.data;
  return (
    <Tile label="Coordinator">
      <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {p === undefined ? (
          <Pill tone="x" dot>
            {probe.isError ? "Not read" : "Asking…"}
          </Pill>
        ) : (
          <Pill tone={p.coordinator === "up" ? "g" : p.coordinator === "starting" ? "a" : "r"} dot>
            {p.coordinator === "up" ? "Up" : p.coordinator === "starting" ? "Starting" : "Unreachable"}
          </Pill>
        )}
        {p?.workers != null && (
          <>
            <span style={{ fontSize: 20, fontWeight: 600 }} className="mono">
              {p.workers}
            </span>
            <span className="sm muted">{p.workers === 1 ? "worker" : "workers"}</span>
          </>
        )}
      </span>
      <span className="sm muted">
        {p !== undefined ? `Asked at ${clock(p.at)} — already stale. ` : ""}Each check is a request to the
        coordinator.
      </span>
      <button type="button" className="btn" style={{ alignSelf: "flex-start" }} onClick={() => void probe.refetch()} disabled={probe.isFetching}>
        Check again
      </button>
    </Tile>
  );
}

function CandidateTile() {
  const review = useReview().data;
  const authorship = useAuthorship(review);
  if (review === undefined) return <Tile label="Candidate">…</Tile>;
  const counts = candidateCounts(review);
  const people = [
    ...new Set(
      [...(authorship?.values() ?? [])].flatMap((a) => (a.kind === "unknown" ? [] : [a.mine ? "you" : a.actor])),
    ),
  ];
  const rollout = review.cost.rollout_sections ?? [];
  return (
    <Tile label="Candidate">
      <span style={{ fontSize: 20, fontWeight: 600 }}>{counts.total === 0 ? "Nothing staged" : plural(counts.total, "change")}</span>
      <span className="sm muted">
        {counts.total === 0
          ? `Matches ${review.base_snapshot == null ? "the empty configuration" : `Snapshot ${review.base_snapshot}`}.`
          : [
              people.length > 0 ? `staged by ${people.join(", ").replace(/, ([^,]*)$/, " and $1")}` : null,
              rollout.length > 0
                ? `${counts.rollout} force a Rollout of ${rollout.map(sectionLabel).join(", ").replace(/, ([^,]*)$/, " and $1")}`
                : "no restart needed",
            ]
              .filter(Boolean)
              .join(" · ")}
      </span>
    </Tile>
  );
}

function ExpiringTile() {
  const certs = useCertificates();
  const list = certs.data ?? [];
  const soon = list.filter((c) => c.status === "expiring" || c.status === "expired");
  const first = [...soon].sort((a, b) => (a.not_after < b.not_after ? -1 : 1))[0];
  return (
    <Tile label="Expiring certificates">
      <span style={{ fontSize: 20, fontWeight: 600 }} className="mono">
        {certs.data === undefined ? "…" : soon.length}
      </span>
      <span className="sm muted">
        {first !== undefined ? (
          <>
            <span className="mono">{first.name}</span> {first.status === "expired" ? "expired" : "on"} {date(first.not_after)} ·
            window: {first.expiring_within_days} days
          </>
        ) : certs.data !== undefined && list[0] !== undefined ? (
          `None inside the ${list[0].expiring_within_days}-day window.`
        ) : (
          "No client certificates."
        )}
      </span>
      <Link className="btn" to="/certificates" style={{ alignSelf: "flex-start" }}>
        Open certificates
      </Link>
    </Tile>
  );
}

function StagedTable() {
  const review = useReview();
  const authorship = useAuthorship(review.data);
  const schemas = useSchemas();
  const [open, setOpen] = useState<string | null>(null);
  const r = review.data;
  const rollout = new Set(r?.cost.rollout_sections ?? []);
  const touched = (r?.sections ?? []).filter((s) => (s.changes?.length ?? 0) > 0);
  const driftOnly = (r?.cost.rollout_sections ?? []).filter((s) => !touched.some((t) => t.section === s));

  return (
    <section className="card" style={{ flex: "1 1 640px", minWidth: 0 }}>
      <div className="chd">
        <span className="ct">Staged changes</span>
        <span className="sm muted">every Section, everyone's work · open one to see which field moved</span>
        <span className="sp" />
        <Link className="btn" to="/review">
          Review all
        </Link>
      </div>
      <ErrorNote error={review.error} />
      {r !== undefined && touched.length === 0 && <div className="cp muted">Nothing is staged.</div>}
      {touched.length > 0 && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Section</th>
                <th>Change</th>
                <th>By</th>
                <th className="right">Costs the cluster</th>
              </tr>
            </thead>
            <tbody>
              {touched.flatMap((s) =>
                (s.changes ?? []).map((c, i) => {
                  const k = key(s.section, c.resource);
                  const a = authorship?.get(k);
                  const [mark, color] = MARK[c.change];
                  const expanded = open === k;
                  return (
                    <Fragment key={k}>
                      <tr className="click" onClick={() => setOpen(expanded ? null : k)}>
                        <td style={i === 0 ? { fontWeight: 550 } : undefined} className={i === 0 ? undefined : "muted"}>
                          {i === 0 ? sectionLabel(s.section) : "·"}
                        </td>
                        <td className="mono">
                          <button
                            type="button"
                            aria-expanded={expanded}
                            style={{ all: "unset", cursor: "pointer" }}
                            onClick={(e) => {
                              e.stopPropagation();
                              setOpen(expanded ? null : k);
                            }}
                          >
                            <span style={{ color }} aria-hidden="true">
                              {mark}
                            </span>{" "}
                            <span className="sr-only">{c.change} </span>
                            {resourceLabel(c.resource)}
                          </button>
                        </td>
                        <td className="muted" title={a?.kind === "unknown" ? "The audit trail does not attribute this change." : undefined}>
                          {a === undefined ? "…" : a.kind === "unknown" ? "—" : authorLabel(a)}
                        </td>
                        <td className="right">
                          <CostPill rollout={rollout.has(s.section)} />
                        </td>
                      </tr>
                      {expanded && (
                        <tr style={{ background: "var(--panel-2)" }}>
                          <td colSpan={4} style={{ padding: 0 }}>
                            <FieldTable section={s.section} change={c} schemas={schemas} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                }),
              )}
            </tbody>
          </table>
        </div>
      )}
      {driftOnly.map((s) => (
        <div key={s} className="note" style={{ margin: 0, borderRadius: 0, borderLeft: 0, borderRight: 0, borderBottom: 0 }}>
          <b>{sectionLabel(s)}</b> has nothing staged and is still in the Rollout: the cluster's copy differs
          from what Apchi would write. <Link to="/drift">See drift</Link>
        </div>
      ))}
    </section>
  );
}

function FieldTable({
  section,
  change,
  schemas,
}: {
  section: S["SectionDiff"]["section"];
  change: S["ResourceChange"];
  schemas: ReturnType<typeof useSchemas>;
}) {
  const rows = fieldChanges(change.before, change.after, secretTest(section, change.after ?? change.before, schemas));
  return (
    <div style={{ padding: "8px 14px 10px 32px" }}>
      <table style={{ border: "1px solid var(--line)", borderRadius: 6 }}>
        <thead>
          <tr>
            <th>Field</th>
            <th>Before</th>
            <th>After</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => (
            <tr key={f.path}>
              <td className="mono" style={{ fontSize: 12 }}>
                {f.path}
              </td>
              <td>{f.before !== undefined && <Value value={f.before} secret={f.secret} kind="old" />}</td>
              <td>{f.after !== undefined && <Value value={f.after} secret={f.secret} kind="new" />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecentSnapshots() {
  const snapshots = useSnapshots(4);
  const live = useCluster().data?.snapshot;
  return (
    <section className="card">
      <div className="chd">
        <span className="ct">Recent Snapshots</span>
        <span className="sp" />
        <Link className="btn" to="/snapshots">
          All
        </Link>
      </div>
      <ErrorNote error={snapshots.error} />
      {snapshots.data?.items.length === 0 && <div className="cp muted">No Snapshot yet.</div>}
      {(snapshots.data?.items.length ?? 0) > 0 && (
        <table className="nw">
          <thead>
            <tr>
              <th>Snapshot</th>
              <th>Committed</th>
              <th>Apply</th>
            </tr>
          </thead>
          <tbody>
            {snapshots.data!.items.map((s) => (
              <tr key={s.number}>
                <td className="mono" style={s.number === live ? { fontWeight: 600 } : undefined}>
                  {s.number} {s.number === live && <Pill tone="g">Current</Pill>}
                </td>
                <td>{dayTime(s.created_at)}</td>
                <td className="mono">
                  <Link to={`/applies/${s.apply_id}`}>{s.apply_id}</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function StateCard() {
  const c = useCluster().data;
  if (c === undefined) return null;
  const last = c.last_apply;
  return (
    <section className="card">
      <div className="chd">
        <span className="ct">State</span>
        <span className="sm muted">from Apchi, no request to Trino</span>
      </div>
      <div className="kv" style={{ padding: "12px 16px", gridTemplateColumns: "auto 1fr", rowGap: 10, alignItems: "center" }}>
        <Pill tone={c.maintenance.engaged ? "r" : "x"}>{c.maintenance.engaged ? "On" : "Off"}</Pill>
        <span>
          Maintenance Mode —{" "}
          {c.maintenance.engaged ? c.maintenance.reason ?? "Operator changes are refused" : "Operators can stage and Apply"}
        </span>
        <Pill tone={c.permissions_enforced ? "g" : "x"}>{c.permissions_enforced ? "On" : "Off"}</Pill>
        <span>
          Permission enforcement —{" "}
          {c.permissions_enforced ? (
            "a Trino Identity reaches only what it has been granted."
          ) : (
            <>
              the default; grants are not restricting anyone yet. <Link to="/permissions">Why this matters</Link>
            </>
          )}
        </span>
        {last != null && (
          <>
            <Link to={`/applies/${last.id}`} style={{ textDecoration: "none" }}>
              <Pill tone={last.stage === "succeeded" ? "g" : last.stage === "failed" || last.stage === "incident" ? "r" : "b"}>
                {last.id}
              </Pill>
            </Link>
            <span>
              Last Apply ·{" "}
              {last.verified === true
                ? "verified"
                : last.verified === false
                  ? "failed Verification"
                  : last.finished_at == null
                    ? `in flight (${last.stage.replace("_", " ")})`
                    : "never reached Verification"}{" "}
              · {dayTime(last.finished_at ?? last.started_at)}
            </span>
          </>
        )}
      </div>
    </section>
  );
}
