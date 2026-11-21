import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { sectionLabel } from "../sections";
import { clock, plural } from "../ui/format";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

const STATUS: Record<string, { tone: "g" | "a" | "x"; label: string; meaning: string }> = {
  in_sync: { tone: "g", label: "In sync", meaning: "Identical to what Apchi would write." },
  diverged: {
    tone: "a",
    label: "Diverged",
    meaning: "Changed outside Apchi, or an Admin value moved. The next Apply rewrites it.",
  },
  unknown: { tone: "x", label: "Unknown", meaning: "The cluster's copy could not be read. Not clean, not dirty." },
  not_comparable: {
    tone: "x",
    label: "Not comparable",
    meaning:
      'Catalogs reach Trino as SQL statements, not a file, so there is nothing to compare. Not "in sync" — unknown by design.',
  },
};

/**
 * Does the cluster still hold what the live Snapshot put there?
 *
 * Read when this screen opens and when somebody asks, never on a timer: it is the most
 * expensive read Apchi offers, and drift is only worth knowing just before an Apply.
 */
export function DriftScreen() {
  const drift = useQuery({
    queryKey: ["drift"],
    queryFn: () => api.GET("/api/v1/cluster/drift").then(unwrap),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const d = drift.data;

  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <PageHead
        title="Drift"
        sub={`Does the cluster still hold what ${d?.snapshot != null ? `Snapshot ${d.snapshot}` : "the live Snapshot"} put there? Read before an Apply, to know whether the restart you are about to pay for is yours.`}
      >
        <button type="button" className="btn pri" disabled={drift.isFetching} onClick={() => void drift.refetch()}>
          {drift.isFetching ? "Reading…" : "Read the cluster again"}
        </button>
      </PageHead>

      <ErrorNote error={drift.error} />
      {d === undefined && !drift.isError && <Loading what="the cluster" />}
      {d !== undefined && d.snapshot == null && (
        <div className="card cp">Nothing has been applied yet, so there is nothing to have drifted from.</div>
      )}
      {d !== undefined && d.snapshot != null && <Verdict drift={d} />}
      {d !== undefined && d.snapshot != null && (
        <section className="card">
          <div className="chd">
            <span className="ct">Per Section</span>
            <span className="sm muted">compared byte for byte with what Apchi would write</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Section</th>
                  <th>Status</th>
                  <th>Reconciling it</th>
                  <th>What it means</th>
                </tr>
              </thead>
              <tbody>
                {d.sections.map((s) => {
                  const st = STATUS[s.status] ?? STATUS.unknown!;
                  const hot = s.status === "diverged";
                  return (
                    <tr key={s.section} style={hot ? { background: "#fffaf2" } : undefined}>
                      <td style={hot ? { fontWeight: 550 } : undefined}>{sectionLabel(s.section)}</td>
                      <td>
                        <Pill tone={st.tone}>{st.label}</Pill>
                      </td>
                      <td>
                        <CostPill rollout={s.restarts_coordinator} />
                      </td>
                      <td className={hot ? undefined : "muted"} style={{ padding: "10px 14px" }}>
                        {st.meaning}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="cft">
            <span className="sm muted">
              Apchi says which Section moved, not which line. Nothing fixes drift except an Apply,
              where the cost is shown and agreed to.
            </span>
            <span className="sp" />
            <Link className="btn" to="/review">
              Review and Apply…
            </Link>
          </div>
        </section>
      )}
      <div className="note note--flush">
        No Section diverged but one unknown reads "<b>Not confirmed</b>", not "in sync" — an
        all-clear nobody could verify is not an all-clear. Apchi does not check on a timer: drift
        is found only when someone reads it here.
      </div>
    </div>
  );
}

function Verdict({ drift }: { drift: S["Drift"] }) {
  const diverged = drift.sections.filter((s) => s.status === "diverged");
  const read = <span className="sm">read {clock(drift.at)} · already stale</span>;

  if (drift.diverged === true) {
    const restarting = diverged.filter((s) => s.restarts_coordinator);
    return (
      <section className="card" style={{ borderColor: "var(--amber-bd)" }}>
        <div className="chd" style={{ background: "var(--amber-bg)", borderColor: "var(--amber-bd)" }}>
          <Pill tone="a">Diverged</Pill>
          <span className="ct" style={{ color: "#7a4705" }}>
            {plural(diverged.length, "Section")} no longer {diverged.length === 1 ? "matches" : "match"} Snapshot{" "}
            {drift.snapshot}
          </span>
          <span className="sp" />
          <span style={{ color: "var(--amber)" }}>{read}</span>
        </div>
        <div className="cp">
          The next Apply rewrites {diverged.length === 1 ? "it" : "them"} whether or not anything is staged there
          {restarting.length > 0 && (
            <>
              {" "}
              — and {restarting.map((s) => sectionLabel(s.section)).join(", ")}{" "}
              {restarting.length === 1 ? "is" : "are"} read only when the coordinator starts, so that Apply
              restarts it and <b>running queries will be terminated</b>
            </>
          )}
          .
        </div>
      </section>
    );
  }
  if (drift.diverged === null) {
    return (
      <section className="card">
        <div className="chd">
          <Pill tone="x">Not confirmed</Pill>
          <span className="ct">No Section is known to have drifted, but not every one could be read</span>
          <span className="sp" />
          <span className="muted">{read}</span>
        </div>
      </section>
    );
  }
  return (
    <section className="card">
      <div className="chd">
        <Pill tone="g">In sync</Pill>
        <span className="ct">The cluster holds what Snapshot {drift.snapshot} put there</span>
        <span className="sp" />
        <span className="muted">{read}</span>
      </div>
    </section>
  );
}
