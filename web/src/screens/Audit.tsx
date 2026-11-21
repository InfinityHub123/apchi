import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { LoadOlder, usePaged } from "../api/paged";
import { sectionLabel } from "../sections";
import { dayTime } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

type Filters = { category: string; actor: string; outcome: string; action: string; resource: string; since: string; until: string };
const EMPTY: Filters = { category: "", actor: "", outcome: "", action: "", resource: "", since: "", until: "" };

const CATEGORIES = ["catalogs", "client_certificates", "certificate_mapping", "permissions", "resource_groups", "event_listeners", "admin", "pipeline", "audit"];
const ACTIONS = ["attempted", "created", "updated", "removed", "reset", "imported", "reverted", "applied", "rolled_back", "engaged", "released", "minted", "revoked", "exported"];

/** Turns the form into the query both the listing and the export take. */
function query(f: Filters) {
  const q: Record<string, string> = {};
  for (const [k, v] of Object.entries(f)) if (v !== "") q[k] = k === "since" || k === "until" ? new Date(v).toISOString() : v;
  return q;
}

/** "jo · sso", "jo · token ci-pipeline", "apchi" -- who acted and how they got in. */
function who(e: S["AuditEvent"]) {
  if (e.actor === "apchi") return <span title="Apchi acted itself: an Auto Rollback, or recovery at start-up">apchi</span>;
  if (e.via === "token") return <>{e.actor} <span className="sm muted">· token {e.token_name ?? e.token_id ?? ""}</span></>;
  return <>{e.actor} <span className="sm muted">· {e.via}</span></>;
}

/** Who did what, and when -- never the values themselves. */
export function AuditScreen() {
  const [form, setForm] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const q = query(applied);
  const paged = usePaged(["audit", q], (before) =>
    api.GET("/api/v1/audit", { params: { query: { ...q, limit: 100, before } as never } }).then(unwrap),
  );
  const exportHref = `/api/v1/audit/export?${new URLSearchParams(q).toString()}`;
  const set = (k: keyof Filters) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  // Consecutive events of one Apply are grouped under it, which is how the trail reads.
  const groups: Array<{ apply: string | null; events: S["AuditEvent"][] }> = [];
  for (const e of paged.items) {
    const last = groups[groups.length - 1];
    if (e.apply_id != null && last?.apply === e.apply_id) last.events.push(e);
    else groups.push({ apply: e.apply_id ?? null, events: [e] });
  }

  return (
    <div className="page">
      <PageHead title="Audit" sub="Who did what, and when — never the values themselves" />
      <form
        className="card cp"
        style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "flex-end" }}
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(form);
        }}
      >
        <label className="fl" style={{ width: 170 }}>
          Category
          <select className="inp" value={form.category} onChange={set("category")}>
            <option value="">All</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {["admin", "pipeline", "audit"].includes(c) ? c : sectionLabel(c as S["SectionDiff"]["section"])}
              </option>
            ))}
          </select>
        </label>
        <label className="fl" style={{ width: 140 }}>
          Actor
          <input className="inp" value={form.actor} onChange={set("actor")} />
        </label>
        <label className="fl" style={{ width: 130 }}>
          Outcome
          <select className="inp" value={form.outcome} onChange={set("outcome")}>
            <option value="">All</option>
            <option value="succeeded">succeeded</option>
            <option value="refused">refused</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <label className="fl" style={{ width: 140 }}>
          Action
          <select className="inp" value={form.action} onChange={set("action")}>
            <option value="">All</option>
            {ACTIONS.map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
        </label>
        <label className="fl" style={{ width: 160 }}>
          Resource
          <input className="inp mono" value={form.resource} onChange={set("resource")} />
        </label>
        <label className="fl">
          From
          <input className="inp" type="datetime-local" value={form.since} onChange={set("since")} />
        </label>
        <label className="fl">
          To
          <input className="inp" type="datetime-local" value={form.until} onChange={set("until")} />
        </label>
        <button type="submit" className="btn pri">
          Filter
        </button>
        <span className="sp" />
        <a className="btn" href={exportHref} download>
          Export what's filtered · NDJSON
        </a>
      </form>
      <span className="sm muted">
        Filters combine — every one must match. A resource filter leaves out acts on a whole Section or the Cluster. Exporting is itself
        recorded here; paging this list is not.
      </span>
      <ErrorNote error={paged.error} />
      {!paged.loaded && !paged.error && <Loading what="the trail" />}
      {paged.loaded && paged.items.length === 0 && <div className="card cp muted">Nothing matches.</div>}
      {paged.items.length > 0 && (
        <section className="card">
          <div className="tbl-wrap">
            <table className="nw">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Actor</th>
                  <th>Category</th>
                  <th>Action</th>
                  <th>Resource</th>
                  <th>Outcome</th>
                  <th>Apply</th>
                  <th>Snapshot</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g, gi) => (
                  <Fragment key={gi}>
                    {g.apply !== null && g.events.length > 1 && (
                      <tr>
                        <td colSpan={9} style={{ background: "var(--panel-2)", height: 32, fontWeight: 600, fontSize: 12 }}>
                          Apply <Link className="mono" to={`/applies/${g.apply}`}>{g.apply}</Link> · {g.events.length} entries
                        </td>
                      </tr>
                    )}
                    {g.events.map((e) => (
                      <tr key={e.id}>
                        <td>{dayTime(e.at)}</td>
                        <td>{who(e)}</td>
                        <td>{e.category}</td>
                        <td>{e.action}</td>
                        <td className={e.resource ? "mono" : "muted"}>{e.resource ?? "—"}</td>
                        <td>
                          <Pill tone={e.outcome === "succeeded" ? "g" : e.outcome === "refused" ? "a" : "r"}>{e.outcome}</Pill>
                        </td>
                        <td className="mono">{e.apply_id ? <Link to={`/applies/${e.apply_id}`}>{e.apply_id}</Link> : <span className="muted">—</span>}</td>
                        <td className="mono">{e.snapshot ?? <span className="muted">—</span>}</td>
                        <td style={{ whiteSpace: "normal", minWidth: 220 }}>{e.detail ?? ""}</td>
                      </tr>
                    ))}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          <LoadOlder paged={paged} />
        </section>
      )}
      <div className="note note--flush">
        A refusal is somebody being correctly told no, not an incident. <span className="mono">apchi</span> means Apchi acted itself — an
        Auto Rollback, or recovery at start-up. A token shows by the name it had when it acted, revoked or not.
      </div>
    </div>
  );
}
