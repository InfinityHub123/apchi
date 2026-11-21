import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiError, api, done, unwrap, type S } from "../api/client";
import { useSchemas } from "../api/queries";
import { sectionLabel } from "../sections";
import { Dialog } from "../ui/Dialog";
import { dayTime, plural } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

type Reading = {
  section: S["SectionDiff"]["section"];
  readable: boolean;
  looked_at: string[];
  guessed_because: string | null;
  resources: Record<string, unknown>;
  amended: string[];
  problems: S["Problem"][];
};

const STEPS = [
  ["Discover", "Read the coordinator's files. Changes nothing."],
  ["Fill the gaps", "Supply what Trino can't report."],
  ["Seed", "Write Apchi's own Secrets. No restart."],
  ["Cut over", "You point the deployment at those Secrets."],
  ["Adopt", "A real Apply and Verification → Snapshot 1."],
] as const;

/**
 * Onboarding a cluster configured before Apchi: read what it runs, fill the gaps, then take
 * it over with a real Apply that produces Snapshot 1. Every step but the last is reversible,
 * and nothing before Adopt changes what the cluster serves.
 */
export function AdoptionScreen() {
  const queries = useQueryClient();
  const navigate = useNavigate();
  const discovery = useQuery({
    queryKey: ["admin", "discovery"],
    queryFn: async () => {
      try {
        return await api.GET("/api/v1/admin/adoption/discovery").then(unwrap);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
  });
  const refresh = () => queries.invalidateQueries({ queryKey: ["admin", "discovery"] });
  const discover = useMutation({ mutationFn: () => api.POST("/api/v1/admin/adoption/discoveries").then(unwrap), onSuccess: refresh });
  const discard = useMutation({ mutationFn: () => api.DELETE("/api/v1/admin/adoption/discovery").then(done), onSuccess: refresh });
  const seed = useMutation({ mutationFn: () => api.POST("/api/v1/admin/adoption/seed").then(unwrap) });
  const adopt = useMutation({
    mutationFn: () => api.POST("/api/v1/admin/adoption/adopt").then(unwrap),
    onSuccess: (record) => navigate(`/apply?id=${record.id}`),
  });
  const [supplying, setSupplying] = useState<string | null>(null);
  const d = discovery.data;
  const step = d == null ? 1 : !d.complete ? 2 : seed.data === undefined ? 3 : 4;
  const sections = (d?.sections ?? []) as Reading[];

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <PageHead title="Adoption" sub="Onboard a cluster that was configured before Apchi: read what it runs, fill the gaps, then take it over with a real Apply that produces Snapshot 1">
        <button type="button" className="btn" disabled={discover.isPending} onClick={() => discover.mutate()}>
          {d == null ? "Read the cluster" : "Re-read cluster"}
        </button>
        {d != null && (
          <button type="button" className="btn dng" disabled={discard.isPending} onClick={() => discard.mutate()}>
            Discard discovery
          </button>
        )}
      </PageHead>
      <ol style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 10, listStyle: "none", padding: 0, margin: 0 }}>
        {STEPS.map(([title, sub], i) => (
          <li key={title} className="card cp" aria-current={step === i + 1 ? "step" : undefined} style={step === i + 1 ? { borderColor: "var(--accent)" } : undefined}>
            <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className={`dot-n ${step > i + 1 ? "g" : step === i + 1 ? "b" : ""}`}>{step > i + 1 ? "✓" : i + 1}</span>
              <b>{title}</b>
            </span>
            <div className="sm muted" style={{ marginTop: 6 }}>
              {i === 1 && d != null && !d.complete ? `${plural(d.outstanding.length, "gap")} left.` : sub}
            </div>
          </li>
        ))}
      </ol>
      <ErrorNote error={discovery.error ?? discover.error ?? discard.error} />
      {discovery.data === undefined && !discovery.error && <Loading what="the discovery" />}
      {d === null && (
        <section className="card cp">
          Nothing has been read yet. <b>Read the cluster</b> reads the coordinator's configuration files and changes nothing.
        </section>
      )}
      {d != null && (
        <>
          <section className="card">
            <div className="chd">
              <span className="ct">What Apchi read</span>
              <span className="sm muted">{dayTime(d.read_at)}</span>
              <span className="sp" />
              {d.complete ? <Pill tone="g">Complete</Pill> : <Pill tone="a">Not complete · {d.outstanding.length} outstanding</Pill>}
            </div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Section</th>
                    <th>Read from</th>
                    <th>Found</th>
                    <th>Supplied by you</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {sections.map((s) => (
                    <tr key={s.section}>
                      <td>{sectionLabel(s.section)}</td>
                      <td className="mono sm" style={{ whiteSpace: "normal" }}>
                        {s.looked_at.join(", ") || "—"}
                        {s.guessed_because && <div className="muted">guessed: {s.guessed_because}</div>}
                      </td>
                      <td className="mono">{Object.keys(s.resources).length}</td>
                      <td className="mono">{s.amended.length || "—"}</td>
                      <td>{!s.readable ? <Pill tone="r">Unreadable</Pill> : s.problems.length > 0 ? <Pill tone="a">{plural(s.problems.length, "problem")}</Pill> : <Pill tone="g">Read</Pill>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {d.changed_since_last_read.length > 0 && (
              <div className="note note--amber" style={{ marginTop: 12 }}>
                Changed on the cluster since the previous reading: {d.changed_since_last_read.join(", ")}.
              </div>
            )}
          </section>
          {d.outstanding.length > 0 && (
            <section className="card">
              <div className="chd">
                <span className="ct">Outstanding</span>
                <span className="sm muted">Seed and Adopt wait for these</span>
              </div>
              {d.outstanding.map((p, i) => {
                // An `incomplete` catalog's path is its name: Trino serves it and its properties cannot
                // be read back, so they are the one gap an Admin fills in from here.
                const catalog = p.kind === "incomplete" && p.path ? p.path : undefined;
                return (
                  <div className="cr" key={i} style={{ gridTemplateColumns: "auto minmax(0,1fr) auto" }}>
                    <Pill tone={p.kind === "unreadable" ? "r" : "a"}>{p.kind}</Pill>
                    <span>
                      {p.section && <b>{sectionLabel(p.section)}</b>} {p.path && <span className="mono">{p.path}</span>} — {p.detail}
                    </span>
                    {catalog !== undefined && (
                      <button type="button" className="btn btn--sm" onClick={() => setSupplying(catalog)}>
                        Supply properties
                      </button>
                    )}
                  </div>
                );
              })}
            </section>
          )}
          {Object.keys(d.admin_values).length > 0 && (
            <section className="card">
              <div className="chd">
                <span className="ct">Becomes Admin values</span>
              </div>
              <div className="cp">
                What the cluster runs that Apchi's Sections can't express — kept working as Admin values, so nobody loses access on adoption
                day:{" "}
                {Object.entries(d.admin_values).map(([k, v]) => (
                  <Pill key={k} tone="x">
                    {k} · {Array.isArray(v) ? v.length : typeof v === "object" && v !== null ? Object.keys(v).length : 1}
                  </Pill>
                ))}
              </div>
            </section>
          )}
          <div className="row">
            <section className="card" style={{ flex: "1 1 300px" }}>
              <div className="chd">
                <span className="ct">3 · Seed</span>
                <span className="sp" />
                {seed.data ? <Pill tone="g">Done</Pill> : <Pill tone="x">{d.complete ? "Ready" : "Waiting"}</Pill>}
              </div>
              <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <span className="sm">Writes Apchi's own Secrets from the discovery. No mount, no restart, no Snapshot — the cluster keeps reading its old files.</span>
                <button type="button" className="btn" disabled={!d.complete || seed.isPending} onClick={() => seed.mutate()}>
                  Seed
                </button>
                <ErrorNote error={seed.error} />
              </div>
            </section>
            <section className="card" style={{ flex: "1 1 300px" }}>
              <div className="chd">
                <span className="ct">4 · Cut over</span>
                <span className="sp" />
                <Pill tone="a">Your step</Pill>
              </div>
              <div className="cp sm" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {seed.data === undefined ? (
                  <span>After seeding, Apchi lists exactly what your deployment must change. Apchi does not change it for you.</span>
                ) : (
                  (seed.data.cutover ?? []).map((c, i) => (
                    <div key={i}>
                      • Mount <span className="mono">{c.secret ?? "—"}</span> at <span className="mono">{c.path}</span>
                      {c.whole_volume && " as a whole volume"} — <span className="muted">{c.why}</span>
                    </div>
                  ))
                )}
              </div>
            </section>
            <section className="card" style={{ flex: "1 1 300px" }}>
              <div className="chd">
                <span className="ct">5 · Adopt</span>
              </div>
              <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <span className="sm">Saves the Admin values, stages the discovery into the Candidate and runs a normal Apply. Snapshot 1 exists only if Verification passes.</span>
                <button type="button" className="btn pri" disabled={!d.complete || adopt.isPending} onClick={() => adopt.mutate()}>
                  Adopt and apply
                </button>
                <span className="hint">Refused if the cluster already has Snapshots, or if Apchi has seen this cluster commit Snapshots before.</span>
                <ErrorNote error={adopt.error} />
              </div>
            </section>
          </div>
        </>
      )}
      <SupplyCatalog name={supplying} onClose={() => setSupplying(null)} onDone={refresh} />
    </div>
  );
}

/** Properties Trino serves but cannot report back, supplied by hand and checked as a new Catalog. */
function SupplyCatalog({ name, onClose, onDone }: { name: string | null; onClose: () => void; onDone: () => unknown }) {
  const schemas = useSchemas();
  const [connector, setConnector] = useState("");
  const [text, setText] = useState("");
  const parse = () =>
    Object.fromEntries(
      text
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l !== "" && !l.startsWith("#"))
        .map((l) => {
          const i = l.indexOf("=");
          return [l.slice(0, i).trim(), l.slice(i + 1).trim()];
        }),
    );
  const save = useMutation({
    mutationFn: () =>
      api.PUT("/api/v1/admin/adoption/discovery/catalogs/{name}", { params: { path: { name: name! } }, body: { name: name!, connector, properties: parse() } }).then(unwrap),
    onSuccess: async () => {
      await onDone();
      onClose();
    },
  });
  return (
    <Dialog
      open={name !== null}
      onClose={onClose}
      title={`Supply properties · ${name ?? ""}`}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn pri" disabled={connector === "" || save.isPending} onClick={() => save.mutate()}>
            Save to discovery
          </button>
        </>
      }
    >
      <span className="sm muted">Checked exactly as a new catalog would be. Apchi will not invent these.</span>
      <label className="fl">
        Connector
        <input className="inp mono" list="adopt-connectors" value={connector} onChange={(e) => setConnector(e.target.value)} />
        <datalist id="adopt-connectors">
          {(schemas.connectors ?? []).map((c) => (
            <option key={c.name} value={c.name} />
          ))}
        </datalist>
      </label>
      <label className="fl">
        Properties <span className="hint">one per line, as in a .properties file</span>
        <textarea className="inp mono" value={text} onChange={(e) => setText(e.target.value)} placeholder={"hive.metastore.uri=thrift://metastore:9083"} />
      </label>
      <ErrorNote error={save.error} />
    </Dialog>
  );
}
