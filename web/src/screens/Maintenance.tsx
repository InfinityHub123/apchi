import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import { usePaged } from "../api/paged";
import { useCluster } from "../api/queries";
import { useIdentity } from "../auth/useAuth";
import { MaintenanceCard } from "./Applies";
import { dayTime } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

/** Freezes Operator changes while the platform is worked on. Readable by everyone. */
export function MaintenanceScreen() {
  const cluster = useCluster();
  const admin = useIdentity().data?.role === "admin";
  const queries = useQueryClient();
  const [reason, setReason] = useState("");
  const engage = useMutation({
    mutationFn: () => api.PUT("/api/v1/admin/maintenance-mode", { body: { engaged: true, reason: reason.trim() } }).then(unwrap),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["cluster"] }),
  });
  const history = usePaged(["audit", "maintenance"], (before) =>
    api.GET("/api/v1/audit", { params: { query: { resource: "maintenance_mode", limit: 25, before } } }).then(unwrap),
  );
  const m = cluster.data?.maintenance;

  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <PageHead
        title={
          <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
            Maintenance Mode {m && <Pill tone={m.engaged ? "r" : "x"}>{m.engaged ? "On" : "Off"}</Pill>}
          </span>
        }
        sub="Freezes Operator changes while the platform is being worked on · engages by itself after a failed Auto Rollback"
      />
      <div className="row">
        <div style={{ flex: "1 1 360px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {m === undefined ? (
            <Loading what="Maintenance Mode" />
          ) : m.engaged ? (
            <MaintenanceCard admin={admin} />
          ) : admin ? (
            <section className="card">
              <div className="chd">
                <span className="ct">Engage</span>
              </div>
              <form
                className="cp"
                style={{ display: "flex", flexDirection: "column", gap: 10 }}
                onSubmit={(e) => {
                  e.preventDefault();
                  engage.mutate();
                }}
              >
                <label className="fl">
                  Reason
                  <input className="inp" required value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Trino upgrade" />
                  <span className="hint">Shown to every Operator whose change is refused.</span>
                </label>
                <button type="submit" className="btn dng-solid" disabled={engage.isPending || reason.trim() === ""}>
                  Engage Maintenance Mode
                </button>
                <ErrorNote error={engage.error} />
              </form>
            </section>
          ) : (
            <section className="card cp">
              <b>Off.</b> <span className="muted">Operators can stage, Validate and Apply. Only an Admin can engage it.</span>
            </section>
          )}
          <section className="card">
            <div className="chd">
              <span className="ct">While it is on</span>
            </div>
            <div className="cp kv" style={{ gridTemplateColumns: "auto 1fr", alignItems: "start" }}>
              <Pill tone="r">Refused</Pill>
              <span>Every Operator change — staging, Reset, Apply — with a message giving the reason</span>
              <Pill tone="g">Still works</Pill>
              <span>Reading everything, and Validation (it changes nothing)</span>
              <Pill tone="g">Still works</Pill>
              <span>Every Admin operation, including releasing it</span>
            </div>
          </section>
        </div>
        <section className="card" style={{ flex: "999 1 480px", minWidth: 0 }}>
          <div className="chd">
            <span className="ct">History</span>
            <span className="sm muted">from the audit</span>
          </div>
          <ErrorNote error={history.error} flush={false} />
          {history.items.length === 0 ? (
            <div className="cp muted">{history.loaded ? "Never engaged." : "Reading…"}</div>
          ) : (
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Action</th>
                    <th>By</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {history.items.map((e) => (
                    <tr key={e.id}>
                      <td style={{ whiteSpace: "nowrap" }}>{dayTime(e.at)}</td>
                      <td>{e.action}</td>
                      <td>{e.actor === "apchi" ? "apchi, automatically" : e.actor}</td>
                      <td style={{ padding: "8px 14px" }}>{e.detail?.replace(/^engaged by \w+: /, "") ?? <span className="muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
