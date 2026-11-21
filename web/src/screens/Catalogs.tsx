import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, done, unwrap, type S } from "../api/client";
import { useCertificates, useInvalidateCandidate, useResetResource, useSchemas } from "../api/queries";
import { CatalogForm } from "../catalogs/CatalogForm";
import { CONNECTOR_INFO, connectorName } from "../catalogs/connectors";
import { authorLabel } from "../review/authorship";
import { fieldChanges, secretTest } from "../review/diff";
import { Value } from "../review/ChangeCard";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState, useSnapshotSection } from "../sections/useSectionState";
import { Dialog } from "../ui/Dialog";
import { clock, daysUntil, plural } from "../ui/format";
import { ChangePill, CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

export function CatalogsScreen() {
  const { name, action } = useParams();
  if (name === "new") return <AddCatalog />;
  if (name !== undefined && action === "edit") return <EditCatalog name={name} />;
  return <CatalogList selected={name ?? null} />;
}

type Row = { name: string; connector: string; supported: boolean; certificate: string | null; change: S["ResourceChange"] | undefined };

function useCatalogs() {
  return useQuery({
    queryKey: ["section", "catalogs"],
    queryFn: () => api.GET("/api/v1/catalogs").then(unwrap),
  });
}

function CatalogList({ selected }: { selected: string | null }) {
  const navigate = useNavigate();
  const catalogs = useCatalogs();
  const state = useSectionState("catalogs");
  const certs = useCertificates();
  const schemas = useSchemas();
  const [view, setView] = useState<"candidate" | "live">("candidate");
  const [filter, setFilter] = useState("");
  const live = useSnapshotSection(state.live, "catalogs", view === "live");
  const curated = new Set((schemas.connectors ?? []).map((c) => c.name));

  const candidateRows: Row[] = [
    ...(catalogs.data ?? []).map((c) => ({
      name: c.name,
      connector: c.connector,
      supported: c.supported,
      certificate: c.certificate ?? null,
      change: state.changes.get(c.name),
    })),
    // A removed Catalog is no longer in the listing, but it is a staged change somebody has
    // to be able to see -- and undo -- from here.
    ...[...state.changes.values()]
      .filter((c) => c.change === "removed")
      .map((c) => {
        const before = (c.before ?? {}) as Record<string, unknown>;
        const connector = String(before.connector ?? "");
        return { name: c.resource, connector, supported: curated.has(connector), certificate: (before.certificate as string) ?? null, change: c };
      }),
  ].sort((a, b) => Number(b.change !== undefined) - Number(a.change !== undefined) || a.name.localeCompare(b.name));

  const liveRows: Row[] = Object.entries(live.data ?? {}).map(([n, v]) => {
    const c = v as Record<string, unknown>;
    return { name: n, connector: String(c.connector), supported: curated.has(String(c.connector)), certificate: (c.certificate as string) ?? null, change: undefined };
  });

  const rows = (view === "live" ? liveRows : candidateRows).filter((r) => r.name.includes(filter.trim().toLowerCase()));
  const staged = candidateRows.filter((r) => r.change !== undefined).length;
  const certOf = new Map((certs.data ?? []).map((c) => [c.name, c]));
  const current = selected === null ? null : candidateRows.find((r) => r.name === selected) ?? null;

  return (
    <div className="page">
      <SectionHead
        name="catalogs"
        sub="The systems Trino can query · catalog changes reach the running cluster by SQL, with no restart"
        rollout={state.rollout}
        view={view}
        onView={setView}
        live={state.live}
        locked={state.locked}
      >
        <Link className="btn pri" to="/catalogs/new" aria-disabled={state.locked} onClick={(e) => state.locked && e.preventDefault()}>
          + Add catalog
        </Link>
      </SectionHead>
      <ErrorNote error={catalogs.error ?? live.error} />
      <div className="card">
        <div className="chd">
          <input className="inp" style={{ maxWidth: 260 }} placeholder="Filter catalogs" aria-label="Filter catalogs" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <span className="sp" />
          <span className="sm muted">
            {view === "live" ? `${plural(liveRows.length, "catalog")} in Snapshot ${state.live}` : `${plural(catalogs.data?.length ?? 0, "catalog")} · ${staged} staged`}
          </span>
        </div>
        {catalogs.data === undefined && !catalogs.error ? (
          <div className="cp"><Loading what="the catalogs" /></div>
        ) : rows.length === 0 ? (
          <div className="cp muted">{filter ? "No catalog matches." : "No catalogs yet. Add one to stage it."}</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Connector</th>
                  <th>Settings</th>
                  <th>Client certificate</th>
                  <th>Apply behavior</th>
                  <th>State</th>
                  <th aria-label="Open" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const cert = r.certificate === null ? undefined : certOf.get(r.certificate);
                  return (
                    <tr
                      key={r.name}
                      className={`click${r.change ? " stg" : ""}${r.name === selected ? " sel" : ""}`}
                      onClick={() => view === "candidate" && navigate(r.name === selected ? "/catalogs" : `/catalogs/${r.name}`)}
                    >
                      <td>
                        {view === "candidate" ? (
                          <Link className="nm" to={`/catalogs/${r.name}`} onClick={(e) => e.stopPropagation()}>
                            {r.name}
                          </Link>
                        ) : (
                          <span className="nm">{r.name}</span>
                        )}
                      </td>
                      <td className={CONNECTOR_INFO[r.connector] ? undefined : "mono"}>{connectorName(r.connector)}</td>
                      <td>{r.supported ? <Pill tone="g">Checked by Apchi</Pill> : <Pill tone="x">Pass-through</Pill>}</td>
                      <td>
                        {r.certificate === null ? (
                          <span className="muted">—</span>
                        ) : (
                          <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                            <span className="mono">{r.certificate}</span>
                            {cert?.status === "expiring" && <Pill tone="a" dot>{daysUntil(cert.not_after)} days</Pill>}
                            {cert?.status === "expired" && <Pill tone="r" dot>Expired</Pill>}
                          </span>
                        )}
                      </td>
                      <td>
                        <CostPill rollout={false} />
                      </td>
                      <td>
                        {r.change ? <ChangePill change={r.change.change} /> : <span className="muted">{view === "live" ? "Live" : state.base == null ? "—" : `In Snapshot ${state.base}`}</span>}
                      </td>
                      <td aria-hidden="true">›</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {view === "candidate" && current !== null && <CatalogDetail row={current} locked={state.locked} />}
    </div>
  );
}

function CatalogDetail({ row, locked }: { row: Row; locked: boolean }) {
  const state = useSectionState("catalogs");
  const schemas = useSchemas();
  const navigate = useNavigate();
  const reset = useResetResource();
  const invalidate = useInvalidateCandidate();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const catalog = useQuery({
    queryKey: ["section", "catalogs", row.name],
    queryFn: () => api.GET("/api/v1/catalogs/{name}", { params: { path: { name: row.name } } }).then(unwrap),
    enabled: row.change?.change !== "removed",
  });
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/catalogs/{name}", { params: { path: { name: row.name } } }).then(done),
    onSuccess: async () => {
      await invalidate();
      setConfirmDelete(false);
      navigate("/catalogs");
    },
  });
  const author = state.author(row.name);
  const body = (catalog.data ?? row.change?.before ?? {}) as Record<string, unknown>;
  const isSecret = secretTest("catalogs", body, schemas);
  const changed = row.change?.change === "changed" ? fieldChanges(row.change.before, row.change.after, isSecret) : [];
  const changedPaths = new Set(changed.map((c) => c.path));
  const props = Object.entries((body.properties ?? {}) as Record<string, string>);

  return (
    <div className="row">
      <section className="card" style={{ flex: "999 1 560px", minWidth: 0 }} aria-label={row.name}>
        <div className="chd">
          {row.change && <ChangePill change={row.change.change} staged={false} />}
          <span className="ct">
            Catalog · <span className="mono">{row.name}</span>
          </span>
          {row.change && (
            <span className="sm muted">
              {author === undefined ? "" : author.kind === "unknown" ? "author not known" : `staged by ${authorLabel(author)} · ${clock(author.at)}`}
            </span>
          )}
          <span className="sp" />
          <CostPill rollout={false} />
        </div>
        <ErrorNote error={catalog.error} flush={false} />
        {row.change?.change === "removed" ? (
          <div className="note note--red" style={{ marginTop: 15 }}>
            Dropped from the running cluster at the next Apply. Queries that use <span className="mono">{row.name}</span> will fail from then on.
          </div>
        ) : (
          <div style={{ padding: "6px 0" }}>
            {props.length === 0 && <div className="cr muted">No properties.</div>}
            {props.map(([k, v]) => {
              const path = `properties.${k}`;
              const c = changed.find((x) => x.path === path);
              return (
                <div className="cr" key={k}>
                  <span className="muted mono" style={{ fontSize: 11.5, wordBreak: "break-all" }}>{k}</span>
                  <span style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", minWidth: 0 }}>
                    {changedPaths.has(path) && c !== undefined && c.before !== undefined ? (
                      <>
                        <Value value={c.before} secret={c.secret} kind="old" />
                        <span className="muted" aria-hidden="true">→</span>
                        <Value value={v} secret={c.secret} kind="new" />
                      </>
                    ) : (
                      <Value value={v} secret={isSecret(path)} kind={changedPaths.has(path) ? "new" : undefined} />
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        <div className="note">
          {row.change ? "Saved to the Candidate as it was edited. " : ""}Reaches the running cluster by SQL at the next Apply. Passwords are
          masked to avoid showing them on a shared screen — this is not access control.
        </div>
      </section>
      <aside className="card cp" style={{ flex: "1 1 280px", minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
        <div className="lab">Details</div>
        <div className="kv">
          <span className="k">Connector</span>
          <span className="v">{connectorName(row.connector)}</span>
          <span className="k">Settings</span>
          <span className="v">{row.supported ? "Checked by Apchi" : "Pass-through"}</span>
          <span className="k">Apply behavior</span>
          <span className="v">No restart</span>
          <span className="k">Client certificate</span>
          <span className="v mono">{row.certificate ?? "—"}</span>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {row.change?.change !== "removed" && (
            <Link className="btn" to={`/catalogs/${row.name}/edit`} aria-disabled={locked} onClick={(e) => locked && e.preventDefault()}>
              Edit
            </Link>
          )}
          {row.change && (
            <button type="button" className="btn" disabled={locked || reset.isPending} onClick={() => reset.mutate({ section: "catalogs", resource: row.name })}>
              Discard this change
            </button>
          )}
          {row.change?.change !== "removed" && (
            <button type="button" className="btn dng" disabled={locked} onClick={() => setConfirmDelete(true)}>
              Delete
            </button>
          )}
        </div>
        <ErrorNote error={reset.error} />
      </aside>
      <Dialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={`Delete ${row.name}?`}
        actions={
          <>
            <button type="button" className="btn" onClick={() => setConfirmDelete(false)}>
              Keep it
            </button>
            <button type="button" className="btn dng-solid" disabled={remove.isPending} onClick={() => remove.mutate()}>
              Stage the removal
            </button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          This stages the removal. At the next Apply <span className="mono">{row.name}</span> is dropped from the running cluster, and
          queries that use it fail from then on.
        </p>
        <ErrorNote error={remove.error} />
      </Dialog>
    </div>
  );
}

function AddCatalog() {
  const schemas = useSchemas();
  const navigate = useNavigate();
  const [picked, setPicked] = useState<string | null>(null);
  const [staged, setStaged] = useState<string | null>(null);
  const connectors = schemas.connectors;
  const rules = picked === null || picked === "custom" ? null : (connectors?.find((c) => c.name === picked) ?? null);
  const step = staged !== null ? 3 : picked !== null ? 2 : 1;

  return (
    <div className="page">
      <div className="crumb">
        <Link to="/catalogs">Catalogs</Link> <span className="muted">/ Add catalog</span>
      </div>
      <ol style={{ display: "flex", gap: 18, listStyle: "none", padding: 0, margin: 0 }} aria-label="Steps">
        {["Choose a connector", "Configure", "Staged"].map((label, i) => (
          <li key={label} style={{ display: "flex", gap: 8, alignItems: "center" }} aria-current={step === i + 1 ? "step" : undefined}>
            <span className={`dot-n ${step === i + 1 ? "b" : step > i + 1 ? "g" : ""}`}>{step > i + 1 ? "✓" : i + 1}</span>
            <span style={{ fontWeight: step === i + 1 ? 600 : 450, color: step === i + 1 ? "var(--ink)" : "var(--ink-3)" }}>{label}</span>
          </li>
        ))}
      </ol>

      {step === 1 && (
        <>
          <PageHead
            title="Choose a connector"
            sub={`Apchi knows ${connectors?.length ?? "…"} connectors and checks their settings. Anything else can be added as a custom catalog, passed through unchecked.`}
          />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12 }}>
            {[...(connectors ?? []).map((c) => c.name), "custom"].map((id) => {
              const info = CONNECTOR_INFO[id];
              return (
                <button
                  key={id}
                  type="button"
                  className="card cp"
                  style={{ textAlign: "left", cursor: "pointer", display: "flex", flexDirection: "column", gap: 6, font: "inherit" }}
                  onClick={() => setPicked(id)}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span className="mt" style={{ background: "var(--accent-bg)", color: "var(--accent)" }}>{id === "custom" ? "+" : info?.mono ?? id.slice(0, 2)}</span>
                    {id === "custom" ? <Pill tone="x">Pass-through</Pill> : <Pill tone="g">Checked by Apchi</Pill>}
                  </span>
                  <span style={{ fontWeight: 600 }}>{id === "custom" ? "Custom" : connectorName(id)}</span>
                  <span className="sm muted">
                    {id === "custom" ? "Any other connector installed on your Trino. Settings pass through unchecked." : info?.blurb ?? ""}
                  </span>
                </button>
              );
            })}
          </div>
          <span className="sm muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <CostPill rollout={false} /> Catalogs are created on the running cluster by SQL — adding one never restarts the coordinator.
          </span>
        </>
      )}

      {step === 2 && picked !== null && (
        <>
          <PageHead
            title={`New ${picked === "custom" ? "custom" : connectorName(picked)} catalog`}
            sub={rules === null ? "Pass-through — Trino checks these settings during Validation." : "Apchi checks each setting before it is staged."}
          >
            <button type="button" className="btn" onClick={() => setPicked(null)}>
              Change connector
            </button>
          </PageHead>
          <CatalogForm key={picked} rules={rules} connector={picked} onStaged={setStaged} onCancel={() => setPicked(null)} />
        </>
      )}

      {step === 3 && staged !== null && (
        <section className="card">
          <div className="chd">
            <span className="nm">{staged}</span>
            <ChangePill change="added" />
            <CostPill rollout={false} />
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span>
              Catalog in the Candidate — <b>not on the cluster yet</b>.
            </span>
            <span className="muted">
              It reaches the cluster at the next Apply, together with everything else staged. Run a Validation to check the connection
              first.
            </span>
          </div>
          <div className="cft">
            <Link className="btn pri" to="/review">
              Review changes
            </Link>
            <Link className="btn" to="/validations">
              Validate
            </Link>
            <button type="button" className="btn" onClick={() => { setStaged(null); setPicked(null); }}>
              Add another
            </button>
            <span className="sp" />
            <button type="button" className="btn" onClick={() => navigate("/catalogs")}>
              Back to Catalogs
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

function EditCatalog({ name }: { name: string }) {
  const navigate = useNavigate();
  const schemas = useSchemas();
  const catalog = useQuery({
    queryKey: ["section", "catalogs", name],
    queryFn: () => api.GET("/api/v1/catalogs/{name}", { params: { path: { name } } }).then(unwrap),
  });
  const c = catalog.data;
  const rules = c === undefined ? null : (schemas.connectors?.find((r) => r.name === c.connector) ?? null);
  return (
    <div className="page">
      <PageHead crumb={<Link to={`/catalogs/${name}`}>Catalogs / {name}</Link>} title={<>Edit <span className="mono">{name}</span></>} sub="Changes are staged in the Candidate, not applied." />
      <ErrorNote error={catalog.error} />
      {c === undefined || schemas.connectors === undefined ? (
        <Loading what={name} />
      ) : (
        <CatalogForm rules={c.supported ? rules : null} connector={c.connector} initial={c} onStaged={() => navigate(`/catalogs/${name}`)} onCancel={() => navigate(`/catalogs/${name}`)} />
      )}
    </div>
  );
}
