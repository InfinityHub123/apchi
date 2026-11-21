import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, done, unwrap } from "../api/client";
import { useInvalidateCandidate, useSchemas } from "../api/queries";
import { CatalogForm } from "../catalogs/CatalogForm";
import { authorLabel } from "../review/authorship";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState } from "../sections/useSectionState";
import { Dialog } from "../ui/Dialog";
import { ChangePill, CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

function useListeners() {
  return useQuery({ queryKey: ["section", "event_listeners"], queryFn: () => api.GET("/api/v1/event-listeners").then(unwrap) });
}

/** Where Trino reports what queries did. Loaded at coordinator start. */
export function EventListenersScreen() {
  const { name } = useParams();
  if (name === "new") return <AddListener />;
  if (name !== undefined) return <ListenerDetail name={name} />;
  return <ListenerList />;
}

function ListenerList() {
  const state = useSectionState("event_listeners");
  const listeners = useListeners();
  const navigate = useNavigate();
  const removed = [...state.changes.values()].filter((c) => c.change === "removed");
  return (
    <div className="page">
      <SectionHead name="event_listeners" sub="Where Trino reports what its queries did · loaded when the coordinator starts, so changes need a Rollout" rollout={true} locked={state.locked}>
        <Link className="btn pri" to="/event-listeners/new" aria-disabled={state.locked} onClick={(e) => state.locked && e.preventDefault()}>
          + Add listener
        </Link>
      </SectionHead>
      <section className="card">
        <ErrorNote error={listeners.error} flush={false} />
        {listeners.data === undefined && !listeners.error ? (
          <div className="cp"><Loading what="the listeners" /></div>
        ) : listeners.data?.length === 0 && removed.length === 0 ? (
          <div className="cp muted">No event listeners.</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Settings</th>
                  <th>Apply behavior</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {(listeners.data ?? []).map((l) => {
                  const change = state.changes.get(l.name);
                  return (
                    <tr key={l.name} className={`click${change ? " stg" : ""}`} onClick={() => navigate(`/event-listeners/${l.name}`)}>
                      <td>
                        <Link className="nm" to={`/event-listeners/${l.name}`} onClick={(e) => e.stopPropagation()}>
                          {l.name}
                        </Link>
                      </td>
                      <td className="mono">{l.type}</td>
                      <td>{l.supported ? <Pill tone="g">Checked by Apchi</Pill> : <Pill tone="x">Pass-through</Pill>}</td>
                      <td>
                        <CostPill rollout={true} />
                      </td>
                      <td>{change ? <ChangePill change={change.change} /> : <span className="muted">{state.base == null ? "—" : `In Snapshot ${state.base}`}</span>}</td>
                    </tr>
                  );
                })}
                {removed.map((c) => (
                  <tr key={c.resource} className="stg">
                    <td className="nm">{c.resource}</td>
                    <td colSpan={3} className="muted">Removed from the Candidate</td>
                    <td>
                      <ChangePill change="removed" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function ListenerDetail({ name }: { name: string }) {
  const state = useSectionState("event_listeners");
  const schemas = useSchemas();
  const navigate = useNavigate();
  const invalidate = useInvalidateCandidate();
  const listeners = useListeners();
  const [confirm, setConfirm] = useState(false);
  const read = useQuery({
    queryKey: ["section", "event_listeners", name],
    queryFn: () => api.GET("/api/v1/event-listeners/{name}", { params: { path: { name } } }).then(unwrap),
  });
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/event-listeners/{name}", { params: { path: { name } } }).then(done),
    onSuccess: async () => {
      await invalidate();
      navigate("/event-listeners");
    },
  });
  const l = read.data;
  const change = state.changes.get(name);
  const author = state.author(name);
  const rules = l === undefined || !l.supported ? null : (schemas.listeners?.find((r) => r.name === l.type) ?? null);

  return (
    <div className="page">
      <PageHead
        crumb={
          <>
            <Link to="/event-listeners">Event listeners</Link> <span className="muted">/ {name}</span>
          </>
        }
        title={
          <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
            <span className="mono">{name}</span>
            {l && <Pill tone="x">{l.type}</Pill>}
            {l && (l.supported ? <Pill tone="g">Checked by Apchi</Pill> : <Pill tone="x">Pass-through</Pill>)}
            {change && <ChangePill change={change.change} />}
          </span>
        }
        sub="Changes to event listeners are loaded when the coordinator starts — applying needs a Rollout."
      >
        {change && (
          <Link className="btn" to="/review">
            View change
          </Link>
        )}
        <button type="button" className="btn dng" disabled={state.locked} onClick={() => setConfirm(true)}>
          Remove listener
        </button>
      </PageHead>
      <ErrorNote error={read.error} />
      {l === undefined || schemas.listeners === undefined ? (
        !read.error && <Loading what={name} />
      ) : (
        <div className="row">
          <div style={{ flex: "999 1 640px", minWidth: 0 }}>
            <CatalogForm
              key={`${l.name}:${JSON.stringify(l.properties)}`}
              target="listener"
              rules={rules}
              connector={l.type}
              initial={{ name: l.name, properties: l.properties ?? {} }}
              onStaged={() => void read.refetch()}
              onCancel={() => navigate("/event-listeners")}
            />
          </div>
          <aside style={{ flex: "1 1 260px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
            <section className="card cp">
              <div className="lab" style={{ marginBottom: 8 }}>Details</div>
              <div className="kv">
                <span className="k">Type</span>
                <span className="v">{l.type}</span>
                <span className="k">Apply behavior</span>
                <span className="v">Rollout</span>
                <span className="k">In Snapshot</span>
                <span className="v mono">{change?.change === "added" ? "not yet" : (state.base ?? "—")}</span>
                {change && (
                  <>
                    <span className="k">Staged by</span>
                    <span className="v">{authorLabel(author)}</span>
                  </>
                )}
              </div>
            </section>
            <section className="card">
              <div className="chd">
                <span className="lab">All listeners</span>
              </div>
              {(listeners.data ?? []).map((x) => (
                <Link key={x.name} className="cr" to={`/event-listeners/${x.name}`} style={{ gridTemplateColumns: "1fr auto", textDecoration: "none" }}>
                  <span className="nm">{x.name}</span>
                  {state.changes.has(x.name) ? <ChangePill change={state.changes.get(x.name)!.change} staged={false} /> : <Pill tone="x">{x.type}</Pill>}
                </Link>
              ))}
            </section>
          </aside>
        </div>
      )}
      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title={`Remove ${name}?`}
        actions={
          <>
            <button type="button" className="btn" onClick={() => setConfirm(false)}>
              Keep it
            </button>
            <button type="button" className="btn dng-solid" disabled={remove.isPending} onClick={() => remove.mutate()}>
              Stage the removal
            </button>
          </>
        }
      >
        <p style={{ margin: 0 }}>Trino stops reporting through it at the next Apply, which restarts the coordinator: running queries will be terminated.</p>
        <ErrorNote error={remove.error} />
      </Dialog>
    </div>
  );
}

function AddListener() {
  const schemas = useSchemas();
  const navigate = useNavigate();
  const [picked, setPicked] = useState<string | null>(null);
  const rules = picked === null || picked === "custom" ? null : (schemas.listeners?.find((r) => r.name === picked) ?? null);
  return (
    <div className="page">
      <PageHead crumb={<Link to="/event-listeners">Event listeners</Link>} title={picked === null ? "Choose a listener type" : `New ${picked} listener`} sub="Loaded when the coordinator starts: applying it needs a Rollout.">
        {picked !== null && (
          <button type="button" className="btn" onClick={() => setPicked(null)}>
            Change type
          </button>
        )}
      </PageHead>
      {picked === null ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 12 }}>
          {[...(schemas.listeners ?? []).map((r) => r.name), "custom"].map((id) => (
            <button key={id} type="button" className="card cp" style={{ textAlign: "left", cursor: "pointer", font: "inherit", display: "flex", flexDirection: "column", gap: 6 }} onClick={() => setPicked(id)}>
              {id === "custom" ? <Pill tone="x">Pass-through</Pill> : <Pill tone="g">Checked by Apchi</Pill>}
              <span style={{ fontWeight: 600 }} className={id === "custom" ? undefined : "mono"}>
                {id === "custom" ? "Custom" : id}
              </span>
            </button>
          ))}
        </div>
      ) : (
        <CatalogForm key={picked} target="listener" rules={rules} connector={picked} onStaged={(n) => navigate(`/event-listeners/${n}`)} onCancel={() => setPicked(null)} />
      )}
    </div>
  );
}
