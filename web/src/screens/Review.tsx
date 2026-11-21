import { useState } from "react";
import { Link } from "react-router-dom";
import { useCluster, useLatestValidation, useReview } from "../api/queries";
import { useAuthorship, key } from "../review/authorship";
import { ChangeCard } from "../review/ChangeCard";
import { candidateCounts } from "../review/counts";
import { sectionLabel } from "../sections";
import { bytes, clock, plural } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";
import { useChangedSince } from "../review/changedSince";

type Filter = "all" | "none" | "rollout" | "mine";

/**
 * Everything an Apply would ship: every Section and every person's changes, because the
 * Candidate is shared and an Apply promotes all of it.
 */
export function ReviewScreen() {
  const review = useReview();
  const cluster = useCluster();
  const authorship = useAuthorship(review.data);
  const [filter, setFilter] = useState<Filter>("all");

  if (review.error) return <div className="page"><ErrorNote error={review.error} /></div>;
  if (review.data === undefined) return <div className="page"><Loading what="the Candidate" /></div>;

  const r = review.data;
  const counts = candidateCounts(r);
  const rollout = new Set(r.cost.rollout_sections ?? []);
  const base = r.base_snapshot ?? null;
  const locked =
    (cluster.data?.maintenance.engaged ?? false) ||
    (cluster.data?.last_apply != null && cluster.data.last_apply.finished_at == null);
  const touched = r.sections.filter((s) => (s.changes?.length ?? 0) > 0);
  const mine = [...(authorship?.values() ?? [])].filter((a) => a.kind !== "unknown" && a.mine).length;

  const visible = touched.flatMap((s) =>
    (s.changes ?? [])
      .filter((c) => {
        if (filter === "none") return !rollout.has(s.section);
        if (filter === "rollout") return rollout.has(s.section);
        if (filter === "mine") {
          const a = authorship?.get(key(s.section, c.resource));
          return a !== undefined && a.kind !== "unknown" && a.mine;
        }
        return true;
      })
      .map((c) => ({ section: s.section, change: c })),
  );
  const pct = Math.min(100, (r.size_bytes / r.limit_bytes) * 100);

  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <PageHead
        title="Staged changes"
        sub={
          <>
            <div>
              Candidate based on{" "}
              <span className="mono" style={{ color: "var(--ink)" }}>
                {base == null ? "no Snapshot" : `Snapshot ${base}`}
              </span>{" "}
              · {plural(counts.total, "change")} across {plural(touched.length, "Section")} · everything
              here ships together at the next Apply
            </div>
            <div className="sm muted" style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              Candidate size
              <span
                role="meter"
                aria-label="Candidate size"
                aria-valuemin={0}
                aria-valuemax={r.limit_bytes}
                aria-valuenow={r.size_bytes}
                style={{ display: "inline-block", width: 120, height: 6, borderRadius: 3, background: "#eceff3", overflow: "hidden" }}
              >
                <span style={{ display: "block", width: `${Math.max(pct, 1)}%`, height: "100%", background: pct > 80 ? "var(--amber)" : "var(--accent)" }} />
              </span>
              <span className="mono">
                {bytes(r.size_bytes)} of {bytes(r.limit_bytes)}
              </span>
              · shared by everyone staging; a change that would pass the limit is refused
            </div>
          </>
        }
      >
        <Link className="btn pri" to="/apply">
          Apply…
        </Link>
      </PageHead>

      {!r.has_changes ? (
        <div className="card cp">
          <b>Nothing is staged.</b>{" "}
          <span className="muted">
            The Candidate matches {base == null ? "the empty configuration" : `Snapshot ${base}`}. Edit a
            Section to stage a change; it appears here before it reaches Trino.
          </span>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }} role="group" aria-label="Show">
            <FilterChip on={filter === "all"} onClick={() => setFilter("all")} label="All" n={counts.total} />
            <FilterChip on={filter === "none"} onClick={() => setFilter("none")} label="No restart" n={counts.noRestart} />
            <FilterChip on={filter === "rollout"} onClick={() => setFilter("rollout")} label="Rollout" n={counts.rollout} />
            <FilterChip on={filter === "mine"} onClick={() => setFilter("mine")} label="Staged by you" n={mine} />
            <span className="sp" />
            <LastValidation />
          </div>

          {rollout.size > 0 && <RolloutCard review={r} rollout={rollout} />}

          {visible.map(({ section, change }) => (
            <ChangeCard
              key={key(section, change.resource)}
              sectionName={section}
              change={change}
              author={authorship?.get(key(section, change.resource))}
              rollout={rollout.has(section)}
              base={base}
              locked={locked}
            />
          ))}
          <span className="sm muted">
            Unchanged settings are hidden. Secret values are masked until shown — a screen-share
            precaution, not a security control.
          </span>
        </>
      )}
    </div>
  );
}

function FilterChip({ on, onClick, label, n }: { on: boolean; onClick: () => void; label: string; n: number }) {
  return (
    <button type="button" className={`tc${on ? " sel" : ""}`} aria-pressed={on} onClick={onClick}>
      {label} <span className="n">{n}</span>
    </button>
  );
}

function LastValidation() {
  const latest = useLatestValidation();
  const v = latest.data;
  const since = useChangedSince(v?.started_at);
  if (v == null) return null;
  const tone = v.outcome === "passed" ? "g" : v.outcome === "failed" ? "r" : "x";
  const word = v.outcome === "passed" ? "passed" : v.outcome === "failed" ? "failed" : "running";
  return (
    <>
      <Link to={`/validations?id=${v.id}`} style={{ textDecoration: "none" }}>
        <Pill tone={tone} dot>
          Last Validation {word} {clock(v.started_at)}
        </Pill>
      </Link>
      {since !== null && (
        <span className="sm muted">
          {since === 0
            ? "the Candidate has not changed since"
            : `the Candidate changed ${since === 1 ? "once" : `${since} times`} since`}
        </span>
      )}
    </>
  );
}

function RolloutCard({ review, rollout }: { review: import("../api/client").Review; rollout: Set<string> }) {
  const bySection = new Map(review.sections.map((s) => [s.section, s.changes?.length ?? 0]));
  const atRisk = review.cost.queries_at_risk;
  return (
    <section className="card" style={{ borderColor: "var(--amber-bd)" }}>
      <div className="chd" style={{ background: "var(--amber-bg)", borderColor: "var(--amber-bd)" }}>
        <Pill tone="a">Rollout</Pill>
        <span className="ct" style={{ color: "#7a4705" }}>
          The Rollout is for {plural(rollout.size, "Section")}
        </span>
        <span className="sm" style={{ color: "var(--amber)" }}>
          running queries will be terminated
          {atRisk != null && ` · ${atRisk} running now`}
        </span>
      </div>
      {(review.cost.rollout_sections ?? []).map((s, i, all) => {
        const n = bySection.get(s) ?? 0;
        return (
          <div
            key={s}
            className="cr"
            style={{
              gridTemplateColumns: "170px minmax(0,1fr) auto",
              borderBottom: i < all.length - 1 ? "1px solid var(--line-2)" : undefined,
            }}
          >
            <span>{sectionLabel(s)}</span>
            {n > 0 ? (
              <span className="muted">{plural(n, "staged change")} below</span>
            ) : (
              <span>
                <b>Nothing is staged here.</b> What Apchi would write differs from what the cluster
                has mounted — someone changed it by hand, or an Admin value moved. This Apply
                rewrites it.
              </span>
            )}
            {n === 0 ? (
              <Link className="btn" to="/drift">
                Check drift
              </Link>
            ) : (
              <span />
            )}
          </div>
        );
      })}
    </section>
  );
}
