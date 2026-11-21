import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, done, unwrap, type S } from "../api/client";
import { useCluster, useInvalidateCandidate } from "../api/queries";
import { useIdentity } from "../auth/useAuth";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState } from "../sections/useSectionState";
import { Dialog } from "../ui/Dialog";
import { plural } from "../ui/format";
import { ChangePill, Pill } from "../ui/Pill";
import { ErrorNote, Loading } from "../ui/States";

type Privilege = S["GrantWrite"]["privileges"][number];
const PRIVILEGES: Privilege[] = ["SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT"];

type Row = S["Grant"] & { change?: S["ResourceChange"]; before?: Privilege[] };

/** Which Trino Identity may do what, where. */
export function PermissionsScreen() {
  const state = useSectionState("permissions");
  const cluster = useCluster();
  const admin = useIdentity().data?.role === "admin";
  const grants = useQuery({ queryKey: ["section", "permissions"], queryFn: () => api.GET("/api/v1/permissions").then(unwrap) });
  const system = useQuery({ queryKey: ["permissions-system"], queryFn: () => api.GET("/api/v1/permissions/system").then(unwrap), staleTime: Infinity });
  const [filter, setFilter] = useState("");
  const [identity, setIdentity] = useState("");
  const [catalog, setCatalog] = useState("");
  const [editing, setEditing] = useState<Row | "new" | null>(null);
  const [removing, setRemoving] = useState<Row | null>(null);

  const rows: Row[] = [
    ...(grants.data ?? []).map((g) => {
      const change = state.changes.get(g.key);
      const before = (change?.before as S["Grant"] | undefined)?.privileges;
      return { ...g, change, before };
    }),
    ...[...state.changes.values()]
      .filter((c) => c.change === "removed")
      .map((c) => ({ ...(c.before as S["Grant"]), key: c.resource, change: c })),
  ].sort((a, b) => Number(b.change !== undefined) - Number(a.change !== undefined) || a.identity.localeCompare(b.identity) || a.catalog.localeCompare(b.catalog));

  const identities = [...new Set((grants.data ?? []).map((g) => g.identity))].sort();
  const catalogs = [...new Set((grants.data ?? []).map((g) => g.catalog))].sort();
  const shown = rows.filter(
    (r) =>
      (identity === "" || r.identity === identity) &&
      (catalog === "" || r.catalog === catalog) &&
      `${r.identity} ${r.catalog} ${r.schema ?? ""} ${r.table ?? ""}`.toLowerCase().includes(filter.toLowerCase()),
  );
  const enforced = cluster.data?.permissions_enforced;

  return (
    <div className="page">
      <SectionHead
        name="permissions"
        sub={`Which Trino Identity may do what, in which catalog, schema and table · ${plural(grants.data?.length ?? 0, "grant")} · ${plural(identities.length, "Trino Identity", "Trino Identities")}`}
        rollout={state.rollout}
        locked={state.locked}
      >
        <button type="button" className="btn pri" disabled={state.locked} onClick={() => setEditing("new")}>
          + Add grant
        </button>
      </SectionHead>
      {enforced === false && (
        <div className="note note--amber note--flush" style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
          <span style={{ flex: "1 1 400px" }}>
            <b>Enforcement is off.</b> These grants are configuration nothing acts on — every Trino Identity can reach everything. Turning
            enforcement on reaches the cluster at the next Apply, with no restart.
          </span>
          {admin && (
            <Link className="btn" to="/admin/enforcement">
              Manage enforcement
            </Link>
          )}
        </div>
      )}
      <section className="card">
        <div className="chd">
          <input className="inp" style={{ maxWidth: 220 }} placeholder="Filter grants" aria-label="Filter grants" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <select className="inp" style={{ maxWidth: 200 }} aria-label="Identity" value={identity} onChange={(e) => setIdentity(e.target.value)}>
            <option value="">Identity: All</option>
            {identities.map((i) => (
              <option key={i}>{i}</option>
            ))}
          </select>
          <select className="inp" style={{ maxWidth: 200 }} aria-label="Catalog" value={catalog} onChange={(e) => setCatalog(e.target.value)}>
            <option value="">Catalog: All</option>
            {catalogs.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
          <span className="sp" />
          <span className="sm muted">
            {shown.length} of {plural(rows.length, "grant")}
          </span>
        </div>
        <ErrorNote error={grants.error} flush={false} />
        {grants.data === undefined && !grants.error ? (
          <div className="cp"><Loading what="the grants" /></div>
        ) : shown.length === 0 ? (
          <div className="cp muted">{rows.length === 0 ? "No grants yet." : "No grant matches."}</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Trino Identity</th>
                  <th>Catalog</th>
                  <th>Schema</th>
                  <th>Table</th>
                  <th>Privileges</th>
                  <th>State</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.key} className={r.change ? "stg" : undefined}>
                    <td className="mono">{r.identity}</td>
                    <td>
                      <span className="nm">{r.catalog}</span>
                    </td>
                    <td className={r.schema ? "mono" : "muted"}>{r.schema ?? "all schemas"}</td>
                    <td className={r.table ? "mono" : "muted"}>{r.table ?? "all tables"}</td>
                    <td style={{ padding: "6px 14px" }}>
                      <Privileges now={r.change?.change === "removed" ? [] : r.privileges} before={r.change?.change === "changed" ? r.before : r.change?.change === "removed" ? r.privileges : undefined} />
                    </td>
                    <td>{r.change ? <ChangePill change={r.change.change} staged={false} /> : <span className="muted">{state.base == null ? "—" : `In Snapshot ${state.base}`}</span>}</td>
                    <td className="right" style={{ whiteSpace: "nowrap" }}>
                      {r.change?.change !== "removed" && (
                        <>
                          <button type="button" className="btn btn--sm" disabled={state.locked} onClick={() => setEditing(r)}>
                            Edit
                          </button>{" "}
                          <button type="button" className="btn btn--sm dng" disabled={state.locked} onClick={() => setRemoving(r)} aria-label={`Remove grant ${r.key}`}>
                            Remove
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card">
        <div className="chd">
          <span className="ct">Rules Apchi owns</span>
          <Pill tone="x">Read-only</Pill>
          <span className="sp" />
          <span className="sm muted">Nobody can edit these</span>
        </div>
        {(system.data?.rules ?? []).map((r, i) => (
          <div className="cr" key={i} style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
            <b style={{ fontWeight: 600 }}>{r.rule}</b>
            <span className="muted">Why: {r.why}</span>
          </div>
        ))}
      </section>
      <GrantDialog grant={editing} onClose={() => setEditing(null)} />
      <RemoveGrant grant={removing} onClose={() => setRemoving(null)} />
    </div>
  );
}

/** Privileges as pills; with `before`, the removed ones struck in red and the added in green. */
function Privileges({ now, before }: { now: Privilege[]; before?: Privilege[] }) {
  const all = [...new Set([...(before ?? []), ...now])];
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
      {all.map((p) => {
        const added = before !== undefined && !before.includes(p);
        const removed = before !== undefined && !now.includes(p);
        return (
          <span key={p} className={`pill mono ${removed ? "r" : added ? "g" : "x"}`} title={removed ? "removed" : added ? "added" : undefined}>
            {removed ? "− " : added ? "+ " : ""}
            {p}
          </span>
        );
      })}
    </span>
  );
}

function GrantDialog({ grant, onClose }: { grant: Row | "new" | null; onClose: () => void }) {
  const invalidate = useInvalidateCandidate();
  const isNew = grant === "new";
  const existing = grant === "new" || grant === null ? null : grant;
  const [draft, setDraft] = useState<S["GrantWrite"]>({ identity: "", catalog: "", schema: null, table: null, privileges: ["SELECT"] });
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const key = existing?.key ?? (isNew ? "new" : null);
  if (key !== loadedFor) {
    setLoadedFor(key);
    setDraft(
      existing !== null
        ? { identity: existing.identity, catalog: existing.catalog, schema: existing.schema ?? null, table: existing.table ?? null, privileges: existing.privileges }
        : { identity: "", catalog: "", schema: null, table: null, privileges: ["SELECT"] },
    );
  }
  const save = useMutation({
    mutationFn: () =>
      existing !== null
        ? api.PUT("/api/v1/permissions/{key}", { params: { path: { key: existing.key } }, body: { privileges: draft.privileges } }).then(unwrap)
        : api.POST("/api/v1/permissions", { body: draft }).then(unwrap),
    onSuccess: async () => {
      await invalidate();
      save.reset();
      onClose();
    },
  });
  const toggle = (p: Privilege) =>
    setDraft((d) => ({ ...d, privileges: d.privileges.includes(p) ? d.privileges.filter((x) => x !== p) : [...d.privileges, p] }));

  return (
    <Dialog
      open={grant !== null}
      onClose={onClose}
      title={isNew ? "Add a grant" : "Edit privileges"}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn pri" disabled={save.isPending || draft.privileges.length === 0 || draft.identity === "" || draft.catalog === ""} onClick={() => save.mutate()}>
            {save.isPending ? "Staging…" : "Stage"}
          </button>
        </>
      }
    >
      <label className="fl">
        Trino Identity
        <input className="inp mono" disabled={!isNew} value={draft.identity} onChange={(e) => setDraft({ ...draft, identity: e.target.value })} />
      </label>
      <label className="fl">
        Catalog
        <input className="inp mono" disabled={!isNew} value={draft.catalog} onChange={(e) => setDraft({ ...draft, catalog: e.target.value })} />
      </label>
      <div style={{ display: "flex", gap: 10 }}>
        <label className="fl" style={{ flex: 1 }}>
          Schema <span className="hint">empty for all schemas</span>
          <input className="inp mono" disabled={!isNew} value={draft.schema ?? ""} onChange={(e) => setDraft({ ...draft, schema: e.target.value || null })} />
        </label>
        <label className="fl" style={{ flex: 1 }}>
          Table <span className="hint">empty for all tables</span>
          <input className="inp mono" disabled={!isNew || !draft.schema} value={draft.table ?? ""} onChange={(e) => setDraft({ ...draft, table: e.target.value || null })} />
        </label>
      </div>
      <fieldset style={{ border: 0, padding: 0, margin: 0, display: "flex", flexWrap: "wrap", gap: 12 }}>
        <legend className="fl" style={{ marginBottom: 6 }}>Privileges</legend>
        {PRIVILEGES.map((p) => (
          <label key={p} className="check mono">
            <input type="checkbox" checked={draft.privileges.includes(p)} onChange={() => toggle(p)} />
            {p}
          </label>
        ))}
      </fieldset>
      {!isNew && <span className="hint">Which identity, catalog, schema and table a grant names is what addresses it; to change those, remove it and add another.</span>}
      <ErrorNote error={save.error} />
    </Dialog>
  );
}

function RemoveGrant({ grant, onClose }: { grant: Row | null; onClose: () => void }) {
  const invalidate = useInvalidateCandidate();
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/permissions/{key}", { params: { path: { key: grant!.key } } }).then(done),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
  });
  return (
    <Dialog
      open={grant !== null}
      onClose={onClose}
      title="Remove this grant?"
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Keep it
          </button>
          <button type="button" className="btn dng-solid" disabled={remove.isPending} onClick={() => remove.mutate()}>
            Stage the removal
          </button>
        </>
      }
    >
      {grant !== null && (
        <p style={{ margin: 0 }}>
          <span className="mono">{grant.identity}</span> loses {grant.privileges.join(", ")} on <span className="mono">{[grant.catalog, grant.schema, grant.table].filter(Boolean).join(".")}</span> at
          the next Apply — once permissions are enforced.
        </p>
      )}
      <ErrorNote error={remove.error} />
    </Dialog>
  );
}
