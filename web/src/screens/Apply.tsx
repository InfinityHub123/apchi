import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { useApply, useCluster, useReview } from "../api/queries";
import { ApplyStages } from "../applies/ApplyStages";
import { outcomeOf } from "../applies/outcome";
import { useApplyStream } from "../applies/useApplyStream";
import { useIdentity } from "../auth/useAuth";
import { authorLabel, key, othersCount, useAuthorship } from "../review/authorship";
import { candidateCounts } from "../review/counts";
import { section } from "../sections";
import { clockSeconds, duration, plural } from "../ui/format";
import { Icon } from "../ui/Icon";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

/**
 * The most consequential screen in the product: confirming an Apply, then following it.
 *
 * Both halves are one route, keyed by `?id=`, so the progress of an Apply has a URL somebody
 * can reload, share or come back to -- closing the page cancels nothing, and the record is
 * durable.
 */
export function ApplyScreen() {
  const [params] = useSearchParams();
  const cluster = useCluster();
  const id = params.get("id");
  const last = cluster.data?.last_apply;
  // An Apply already in flight is the only one that may run; show it rather than offer a
  // second that the API would refuse.
  const running = last != null && last.finished_at == null && last.stage !== "incident" ? last.id : null;
  const showing = id ?? running;

  return (
    <div className="page">
      <PageHead
        title="Apply"
        sub="Validate, apply, roll out, verify, commit — one pipeline, minutes long. Closing the page cancels nothing."
      />
      <div className="row">
        {showing === null && <Confirm />}
        {showing !== null && <Progress id={showing} />}
      </div>
    </div>
  );
}

function Confirm() {
  const review = useReview();
  const cluster = useCluster();
  const authorship = useAuthorship(review.data);
  const navigate = useNavigate();
  const queries = useQueryClient();
  const start = useMutation({
    mutationFn: () => api.POST("/api/v1/applies").then(unwrap),
    onSuccess: (record) => {
      void queries.invalidateQueries({ queryKey: ["cluster"] });
      navigate(`/apply?id=${record.id}`, { replace: true });
    },
  });

  if (review.error) return <ErrorNote error={review.error} />;
  if (review.data === undefined) return <Loading what="the Candidate" />;

  const r = review.data;
  const counts = candidateCounts(r);
  const rollout = new Set(r.cost.rollout_sections ?? []);
  const others = othersCount(authorship);
  const locked = cluster.data?.maintenance.engaged ?? false;
  const base = r.base_snapshot == null ? "the empty configuration" : `Snapshot ${r.base_snapshot}`;
  const changes = r.sections.flatMap((s) =>
    (s.changes ?? []).map((c) => ({ section: s.section, change: c })),
  );
  // Rollout changes first: they are what this confirmation exists for.
  changes.sort((a, b) => Number(rollout.has(b.section)) - Number(rollout.has(a.section)));

  return (
    <section className="card" style={{ flex: "1 1 520px", minWidth: 0, maxWidth: 820 }} aria-label="Confirm Apply">
      <div className="chd">
        <span className="lab">1 · Before</span>
        <span className="ct">{counts.total === 0 ? "Nothing is staged" : `Apply ${plural(counts.total, "change")}`}</span>
        <span className="sp" />
        {others !== null && others > 0 && <span className="sm muted">{others} staged by others</span>}
      </div>

      {r.cost.restarts_coordinator && (
        <div style={{ padding: "14px 15px", background: "var(--red-bg)", borderBottom: "1px solid var(--red-bd)", color: "var(--red)", display: "flex", flexDirection: "column", gap: 8 }}>
          {/* §21: "running queries will be terminated, in those words". */}
          <span style={{ fontWeight: 600, display: "flex", gap: 8, alignItems: "center" }}>
            <Icon name="alert" size={16} />
            This Apply includes a Rollout. Running queries will be terminated.
          </span>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
            <span style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
              <span className="mono" style={{ fontSize: 22, fontWeight: 600 }}>
                {r.cost.queries_at_risk ?? "?"}
              </span>
              <span className="sm">running or queued</span>
            </span>
            <span className="sm" style={{ color: "#7a1717" }}>
              {r.cost.queries_at_risk == null
                ? "The coordinator could not be asked."
                : <>Read from the cluster at <span className="mono">{clockSeconds(new Date(review.dataUpdatedAt).toISOString())}</span> — a live reading, not a promise.</>}
            </span>
            <button type="button" className="btn btn--sm" onClick={() => void review.refetch()} disabled={review.isFetching}>
              Read again
            </button>
          </div>
        </div>
      )}

      {changes.length > 0 && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Change</th>
                <th>Staged by</th>
                <th>Behavior</th>
              </tr>
            </thead>
            <tbody>
              {changes.map(({ section: s, change: c }) => {
                const a = authorship?.get(key(s, c.resource));
                return (
                  <tr key={key(s, c.resource)}>
                    <td>
                      <b style={{ fontWeight: 600 }}>{section(s).noun}</b> ·{" "}
                      <span className="mono">{c.resource === "#selectors" ? "selectors" : c.resource}</span>{" "}
                      {c.change === "removed" && <Pill tone="r" xs>{s === "catalogs" ? "dropped" : "removed"}</Pill>}
                      {c.change === "added" && <Pill tone="b" xs>added</Pill>}
                    </td>
                    <td className={a?.kind === "unknown" ? "muted" : undefined}>{authorLabel(a)}</td>
                    <td>
                      <CostPill rollout={rollout.has(s)} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {r.cost.warning != null && !r.cost.restarts_coordinator && (
        <div className="note" style={{ marginTop: 15 }}>{r.cost.warning}</div>
      )}
      <div className="note" style={{ marginTop: 15 }}>
        Validation runs again first (1–5 min){r.cost.restarts_coordinator ? ", then the Rollout (2–10 min)" : ""}. The
        Candidate is frozen until it finishes. If anything fails before Commit, no Snapshot is created and {base}{" "}
        stays the known-good configuration.
      </div>
      <ErrorNote error={start.error} flush={false} />
      {locked && (
        <div className="note note--red">Maintenance Mode is on. Applies are refused until an Admin releases it.</div>
      )}
      <div className="cft">
        <span className="sp" />
        <Link className="btn" to="/review">
          Back to changes
        </Link>
        <button
          type="button"
          className={`btn ${r.cost.restarts_coordinator ? "dng-solid" : "pri"}`}
          disabled={start.isPending || locked || counts.total === 0 && !r.cost.restarts_coordinator}
          onClick={() => start.mutate()}
        >
          {start.isPending ? "Starting…" : r.cost.restarts_coordinator ? "Apply and roll out" : "Apply"}
        </button>
      </div>
    </section>
  );
}

function Progress({ id }: { id: string }) {
  const apply = useApply(id);
  const me = useIdentity().data?.subject;
  const record = apply.data;
  const finished = record !== undefined && !outcomeOf(record).inFlight;
  const stream = useApplyStream(id, finished);

  if (apply.error) return <ErrorNote error={apply.error} />;
  if (record === undefined) return <Loading what={`Apply ${id}`} />;
  const o = outcomeOf(record);

  return (
    <div style={{ flex: "1 1 560px", minWidth: 0, maxWidth: 900, display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
        <span className="lab">2 · {o.inFlight ? "During" : "After"}</span>
        <span className="ct mono">{record.id}</span>
        <Pill tone={o.tone} dot={o.inFlight}>
          {o.label}
        </Pill>
        <span className="sp" />
        <span className="sm muted">
          started {clockSeconds(record.started_at)}
          {record.started_by != null && ` by ${record.started_by === me ? "you" : record.started_by}`} ·{" "}
          {duration(record.started_at, record.finished_at)}
        </span>
      </div>
      <ApplyStages record={record} />
      <section className="card">
        <div className="chd">
          <span className="lab">Event stream</span>
          <span className="sp" />
          <span className="sm muted">
            {o.inFlight ? (stream.connected ? "live · reconnects where it left off" : "connecting…") : "the Apply is over"}
          </span>
        </div>
        <pre className="code">
          {(record.history ?? []).map((e) => `${clockSeconds(e.at)}  ${e.stage}${e.detail ? `  ${e.detail}` : ""}`).join("\n")}
        </pre>
      </section>
      {!o.inFlight && (
        <div style={{ display: "flex", gap: 8 }}>
          <Link className="btn" to={`/applies/${record.id}`}>
            Full record
          </Link>
          {record.snapshot != null && (
            <Link className="btn" to="/snapshots">
              Snapshot {record.snapshot}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

export type { S };
