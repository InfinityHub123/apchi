import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, done, unwrap, type S } from "../api/client";
import { LoadOlder, usePaged } from "../api/paged";
import { useApply, useCluster } from "../api/queries";
import { ApplyStages, Checks, OutcomeCard } from "../applies/ApplyStages";
import { outcomeOf, stageLabel } from "../applies/outcome";
import { useIdentity } from "../auth/useAuth";
import { clockSeconds, dayTime } from "../ui/format";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

const VIA_TOKEN = (r: S["ApplyRecord"]) => r.started_via === "token";

/** Every Apply and Admin Apply, newest first. The record survives closing the page. */
export function AppliesScreen() {
  const { id } = useParams();
  if (id !== undefined) return <ApplyDetail id={id} />;
  return <AppliesList />;
}

function AppliesList() {
  const navigate = useNavigate();
  const me = useIdentity().data?.subject;
  const paged = usePaged(["applies"], (before) =>
    api.GET("/api/v1/applies", { params: { query: { limit: 25, before } } }).then(unwrap),
    { refetchInterval: 10_000 },
  );

  return (
    <div className="page">
      <PageHead
        title="Applies"
        sub="Every Apply and Admin Apply, newest first, with its stages and outcome · the record survives closing the page"
      >
        <Link className="btn pri" to="/apply">
          Apply the Candidate…
        </Link>
      </PageHead>
      <ErrorNote error={paged.error} />
      {!paged.loaded && !paged.error && <Loading what="the Applies" />}
      {paged.loaded && paged.items.length === 0 && <div className="card cp muted">Nothing has been applied yet.</div>}
      {paged.items.length > 0 && (
        <section className="card">
          <div className="tbl-wrap">
            <table className="nw">
              <thead>
                <tr>
                  <th>Apply</th>
                  <th>Kind</th>
                  <th>Started</th>
                  <th>By</th>
                  <th>Outcome</th>
                  <th>Restart</th>
                  <th>Snapshot</th>
                  <th>Warnings</th>
                  <th aria-label="Open" />
                </tr>
              </thead>
              <tbody>
                {paged.items.map((r) => {
                  const o = outcomeOf(r);
                  return (
                    <tr key={r.id} className={`click${o.inFlight ? " stg" : ""}`} onClick={() => navigate(`/applies/${r.id}`)}>
                      <td>
                        <Link className="nm" to={`/applies/${r.id}`} onClick={(e) => e.stopPropagation()}>
                          {r.id}
                        </Link>
                      </td>
                      <td>{r.kind === "admin_apply" ? <Pill tone="x">Admin Apply</Pill> : "Apply"}</td>
                      <td>{dayTime(r.started_at)}</td>
                      <td>
                        {r.started_by == null ? <span className="muted">not recorded</span> : r.started_by === me ? "you" : r.started_by}
                        {VIA_TOKEN(r) && <span className="sm muted"> via token</span>}
                      </td>
                      <td>
                        <Pill tone={o.tone} dot={o.inFlight}>
                          {o.label}
                        </Pill>
                      </td>
                      <td>
                        <CostPill rollout={r.rolled_out} />
                      </td>
                      <td>
                        {r.snapshot != null ? (
                          <span className="nm">{r.snapshot}</span>
                        ) : r.kind === "admin_apply" && o.label === "Succeeded" ? (
                          <span className="muted">none — Admin Applies create no Snapshot</span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className={(r.warnings?.length ?? 0) > 0 ? "mono" : "muted"}>{(r.warnings?.length ?? 0) || "—"}</td>
                      <td aria-hidden="true">›</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <LoadOlder paged={paged} />
        </section>
      )}
    </div>
  );
}

function ApplyDetail({ id }: { id: string }) {
  const apply = useApply(id);
  const me = useIdentity().data;
  const r = apply.data;

  if (apply.error) return <div className="page"><ErrorNote error={apply.error} /></div>;
  if (r === undefined) return <div className="page"><Loading what={`Apply ${id}`} /></div>;
  const o = outcomeOf(r);
  const incident = r.stage === "incident";

  return (
    <div className="page">
      <PageHead
        crumb={<Link to="/applies">Applies</Link>}
        title={
          <>
            {incident ? "Incident · " : r.kind === "admin_apply" ? "Admin Apply " : "Apply "}
            {incident && "Apply "}
            <span className="mono">{r.id}</span>
          </>
        }
        sub={
          incident
            ? "Apchi has stopped touching the cluster and will not retry"
            : `${o.label}${r.started_by != null ? ` · started by ${r.started_by === me?.subject ? "you" : r.started_by}` : ""} · ${dayTime(r.started_at)}`
        }
      >
        {o.inFlight && (
          <Link className="btn pri" to={`/apply?id=${r.id}`}>
            Follow it live
          </Link>
        )}
      </PageHead>
      <div className="row">
        <div style={{ flex: "999 1 600px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {incident ? <IncidentBody record={r} /> : <ApplyStages record={r} />}
        </div>
        {incident && (
          <aside style={{ flex: "1 1 300px", minWidth: 0 }}>
            <MaintenanceCard admin={me?.role === "admin"} />
          </aside>
        )}
      </div>
    </div>
  );
}

function IncidentBody({ record }: { record: S["ApplyRecord"] }) {
  return (
    <>
      <section className="card">
        <div className="chd">
          <span className="ct">Stages</span>
          <span className="sp" />
          <span className="sm muted">
            started {clockSeconds(record.started_at)}
            {record.started_by != null && ` by ${record.started_by}`}
          </span>
        </div>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Stage</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {(record.history ?? []).map((e, i) => (
                <tr key={i}>
                  <td className="mono">{clockSeconds(e.at)}</td>
                  <td style={e.stage === "incident" || e.stage === "rolling_back" ? { fontWeight: 600 } : undefined}>
                    {e.stage === "rolling_back" && record.base_snapshot != null
                      ? `Rolling back to Snapshot ${record.base_snapshot}`
                      : stageLabel(e.stage)}
                  </td>
                  <td className={e.detail ? undefined : "muted"} style={{ padding: "8px 14px" }}>
                    {e.stage === "incident" ? <Pill tone="r" dot>Maintenance Mode engaged</Pill> : e.detail ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      {(record.verification?.length ?? 0) > 0 && (
        <section className="card">
          <div className="chd">
            <span className="ct">Verification checks</span>
            <span className="sm muted">in the order they run · stops at the first failure</span>
          </div>
          <Checks checks={record.verification ?? []} />
          <div style={{ height: 12 }} />
        </section>
      )}
      <OutcomeCard record={record} />
      <section className="card">
        <div className="chd">
          <span className="lab">For comparison — an ordinary failed Apply</span>
        </div>
        <div className="note" style={{ marginTop: 15 }}>
          After an ordinary failure the cluster is back on its Snapshot and work continues. An incident is
          different: the rollback also failed, and nobody knows what configuration the cluster is running.
        </div>
      </section>
    </>
  );
}

/** Releasing Maintenance Mode after an incident. Admin only; the reason is the record. */
export function MaintenanceCard({ admin }: { admin: boolean }) {
  const cluster = useCluster();
  const queries = useQueryClient();
  const [reason, setReason] = useState("");
  const release = useMutation({
    mutationFn: () =>
      api
        .PUT("/api/v1/admin/maintenance-mode", { body: { engaged: false, reason: reason.trim() } })
        .then(done),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["cluster"] }),
  });
  const m = cluster.data?.maintenance;
  if (m === undefined) return null;
  if (!m.engaged) {
    return (
      <section className="card cp">
        <b>Maintenance Mode is off.</b> <span className="muted">Operators can stage and Apply again.</span>
      </section>
    );
  }
  return (
    <section className="card">
      <div className="chd">
        <span className="ct">Maintenance Mode is on</span>
      </div>
      <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="kv">
          <span className="k">Engaged by</span>
          <span className="v">{m.engaged_by === "auto_rollback_failure" ? "Apchi, automatically" : "an Admin"}</span>
          <span className="k">Reason</span>
          <span className="v">{m.reason ?? "—"}</span>
          {m.engaged_at != null && (
            <>
              <span className="k">At</span>
              <span className="v mono">{dayTime(m.engaged_at)}</span>
            </>
          )}
        </div>
        {admin ? (
          <>
            <label className="fl">
              Reason for releasing
              <textarea
                className="inp"
                style={{ minHeight: 80 }}
                placeholder="Why the cluster is safe to configure again"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <button type="button" className="btn" disabled={reason.trim() === "" || release.isPending} onClick={() => release.mutate()}>
              Release Maintenance Mode
            </button>
            <ErrorNote error={release.error} />
          </>
        ) : null}
        <span className="hint">Admin only. Operators can't stage, Validate or Apply while it is on.</span>
      </div>
    </section>
  );
}
