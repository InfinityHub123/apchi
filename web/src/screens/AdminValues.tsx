import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { useCluster } from "../api/queries";
import { filesOf, filled, useAdminApply, useEnforcement, useRawFiles, useServerConfiguration } from "../admin/requests";
import { useQuery } from "@tanstack/react-query";
import { Pill } from "../ui/Pill";
import { ErrorNote, PageHead } from "../ui/States";
import { download } from "../ui/download";

/** Everything an Admin configures outside the Candidate. */
export function AdminValuesScreen() {
  const live = useCluster().data?.snapshot ?? null;
  const enforcement = useEnforcement();
  const files = useRawFiles();
  const server = useServerConfiguration();
  const set = (["coordinator", "worker"] as const).map((role) => ({
    role,
    count: filesOf(role).reduce((n, f) => n + Object.keys(filled(server.data)[role][f] ?? {}).length, 0),
  }));
  const preserved = useQuery({ queryKey: ["admin", "preserved-mappings"], queryFn: () => api.GET("/api/v1/admin/certificate-mapping/preserved").then(unwrap) });
  const apply = useAdminApply();
  const exporting = useMutation({ mutationFn: () => download("/api/v1/admin/configuration/export", "apchi-configuration-with-admin-values.json") });
  // The preserved access-control rules are only in the Admin export, which also carries raw
  // file contents; read on request rather than on every visit, so opening this page does not
  // pull every secret an Admin wrote into the browser.
  const rules = useMutation({
    mutationFn: () =>
      api
        .GET("/api/v1/admin/configuration/export")
        .then(unwrap)
        .then((c) => ((c.admin as { preserved_access_control?: Record<string, unknown[]> } | null)?.preserved_access_control ?? {})),
  });

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <PageHead title="Admin values" sub="Everything an Admin configures outside the Candidate — platform configuration with its own lifecycle">
        <button type="button" className="btn" disabled={exporting.isPending} onClick={() => exporting.mutate()}>
          Export with Admin values
        </button>
        <button type="button" className="btn pri" disabled={apply.isPending} onClick={() => apply.mutate()}>
          Admin Apply…
        </button>
      </PageHead>
      <ErrorNote error={apply.error ?? exporting.error} />
      <section className="card">
        <div className="chd">
          <span className="ct">How Admin values differ from the six Sections</span>
        </div>
        <div className="cp" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16 }}>
          {[
            ["Not in the Candidate", "Edited here, not staged. Operators can't see or change them."],
            ["Never in a Snapshot", "What ran = the Snapshot merged with the Admin values of that moment. They survive a revert or rollback."],
            ["Admin wins", "Where an Admin value and generated configuration set the same property, the Admin value applies."],
            ["Delivered by an Apply", `The next Apply delivers them. An Admin Apply sends ${live == null ? "the latest Snapshot" : `Snapshot ${live}`} + Admin values — never the Candidate — and creates no Snapshot.`],
          ].map(([t, d]) => (
            <div key={t}>
              <div style={{ fontWeight: 600 }}>{t}</div>
              <div className="sm muted">{d}</div>
            </div>
          ))}
        </div>
      </section>
      <section className="card">
        <div className="chd">
          <span className="ct">Typed values</span>
          <span className="sm muted">Apchi understands, validates and merges these into the files it generates</span>
        </div>
        <div className="cr" style={{ gridTemplateColumns: "220px minmax(0,1fr) auto" }}>
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <b>Permission enforcement</b>
            {enforcement.data && <Pill tone={enforcement.data.enforced ? "g" : "x"}>{enforcement.data.enforced ? "On" : "Off"}</Pill>}
          </span>
          <span className="muted">Whether grants restrict anyone. Off: a catch-all lets every Trino Identity reach everything.</span>
          <Link to="/admin/enforcement">Manage →</Link>
        </div>
        <div className="cr" style={{ gridTemplateColumns: "220px minmax(0,1fr) auto" }}>
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <b>Preserved mapping patterns</b> <Pill tone="x">{preserved.data?.patterns?.length ?? "…"}</Pill>
          </span>
          <span className="muted">Certificate-mapping patterns the cluster used before Apchi, checked after the Operator's one pattern while clients migrate.</span>
          <Link to="/certificate-mapping">Edit on Certificate mapping →</Link>
        </div>
        <div className="cr" style={{ gridTemplateColumns: "220px minmax(0,1fr) auto" }}>
          <b>Preserved access-control rules</b>
          <span className="muted">Rules the cluster ran before Apchi managed it, kept working beneath the Operator's grants until the grants catch up. Set and changed through an Admin import.</span>
          <button type="button" className="btn btn--sm" disabled={rules.isPending} onClick={() => rules.mutate()}>
            Show
          </button>
        </div>
        {rules.data !== undefined && (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Block</th>
                  <th>Rules</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(rules.data).length === 0 && (
                  <tr>
                    <td colSpan={2} className="muted">None preserved.</td>
                  </tr>
                )}
                {Object.entries(rules.data).map(([block, list]) => (
                  <tr key={block}>
                    <td className="mono">{block}</td>
                    <td className="mono">{list.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ErrorNote error={rules.error} flush={false} />
      </section>
      <section className="card">
        <div className="chd">
          <span className="ct">Server configuration</span>
          <span className="sm muted">Trino's own files, rendered by Apchi; wiring and sizing are the chart's</span>
        </div>
        <div className="cr" style={{ gridTemplateColumns: "220px minmax(0,1fr) auto" }}>
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <b>Properties</b> <Pill tone="x">{server.data === undefined ? "…" : set.reduce((n, r) => n + r.count, 0)}</Pill>
          </span>
          <span className="muted">
            {server.data === undefined ? "…" : set.map((r) => `${r.role === "worker" ? "workers" : r.role}: ${r.count}`).join(" · ")}
          </span>
          <Link to="/admin/server-configuration">Manage →</Link>
        </div>
      </section>
      <section className="card">
        <div className="chd">
          <span className="ct">Raw files</span>
          <span className="sm muted">delivered as written; Apchi forms no opinion about them</span>
        </div>
        <div className="cr" style={{ gridTemplateColumns: "220px minmax(0,1fr) auto" }}>
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <b>Raw files</b> <Pill tone="x">{files.data?.length ?? "…"}</Pill>
          </span>
          <span className="muted mono">{(files.data ?? []).map((f) => f.path.split("/").pop()).join(" · ") || "none"}</span>
          <Link to="/admin/raw-files">Manage →</Link>
        </div>
      </section>
    </div>
  );
}
