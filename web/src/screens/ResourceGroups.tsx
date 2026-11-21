import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, done, unwrap, type S } from "../api/client";
import { useInvalidateCandidate } from "../api/queries";
import { describeSelector, selectorMoves } from "../review/diff";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState } from "../sections/useSectionState";
import { Dialog } from "../ui/Dialog";
import { ChangePill, Pill } from "../ui/Pill";
import { ErrorNote, Loading } from "../ui/States";

type Group = S["ResourceGroup"];
type Selector = S["Selector"];

/** How queries share CPU, memory and concurrency. Read once at coordinator start. */
export function ResourceGroupsScreen() {
  const state = useSectionState("resource_groups");
  const invalidate = useInvalidateCandidate();
  const groups = useQuery({ queryKey: ["section", "resource_groups", "groups"], queryFn: () => api.GET("/api/v1/resource-groups").then(unwrap) });
  const selectors = useQuery({ queryKey: ["section", "resource_groups", "selectors"], queryFn: () => api.GET("/api/v1/resource-groups/selectors").then(unwrap) });
  const settings = useQuery({ queryKey: ["section", "resource_groups", "settings"], queryFn: () => api.GET("/api/v1/resource-groups/settings").then(unwrap) });
  const [editing, setEditing] = useState<Group | "new" | null>(null);

  const list = [...(groups.data ?? [])].sort((a, b) => a.path.localeCompare(b.path));
  const removed = [...state.changes.values()].filter((c) => c.change === "removed" && !c.resource.startsWith("#"));
  const needsQuota = list.some((g) => g.hard_cpu_limit || g.soft_cpu_limit) && !settings.data?.cpu_quota_period;

  return (
    <div className="page">
      <SectionHead
        name="resource_groups"
        sub="How queries share CPU, memory and concurrency · read once at coordinator start, so changes need a Rollout"
        rollout={true}
        locked={state.locked}
      />
      <div className="row">
        <div style={{ flex: "1 1 520px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          <section className="card">
            <div className="chd">
              <span className="ct">Groups</span>
              <span className="sp" />
              <button type="button" className="btn" disabled={state.locked} onClick={() => setEditing("new")}>
                + Add group
              </button>
            </div>
            <ErrorNote error={groups.error} flush={false} />
            {groups.data === undefined && !groups.error ? (
              <div className="cp"><Loading what="the groups" /></div>
            ) : (
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Group</th>
                      <th>Concurrency</th>
                      <th>Memory</th>
                      <th>CPU</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((g) => {
                      const change = state.changes.get(g.path);
                      return (
                        <tr key={g.path} className={`click${change ? " stg" : ""}`} onClick={() => !state.locked && setEditing(g)}>
                          <td>
                            <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                              <button type="button" className="nm" style={{ all: "unset", cursor: "pointer", fontFamily: "var(--mono)", fontWeight: 600, color: "var(--accent)" }} onClick={() => setEditing(g)} disabled={state.locked}>
                                {g.path}
                              </button>
                              {change && <ChangePill change={change.change} staged={false} />}
                            </span>
                          </td>
                          <td className="mono">{g.hard_concurrency_limit}</td>
                          <td className="mono">{g.soft_memory_limit ?? "—"}</td>
                          <td className="mono">{g.hard_cpu_limit ?? "—"}</td>
                        </tr>
                      );
                    })}
                    {removed.map((c) => (
                      <tr key={c.resource} className="stg">
                        <td>
                          <span className="nm">{c.resource}</span> <ChangePill change="removed" staged={false} />
                        </td>
                        <td colSpan={3} className="muted">Removed from the Candidate</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <QuotaPeriod current={settings.data?.cpu_quota_period ?? null} needed={needsQuota} locked={state.locked} onSaved={invalidate} />
        </div>
        <SelectorList
          selectors={selectors.data?.selectors ?? null}
          error={selectors.error}
          before={state.changes.get("#selectors")?.before}
          groups={list.map((g) => g.path)}
          locked={state.locked}
        />
      </div>
      <GroupDialog group={editing} onClose={() => setEditing(null)} />
    </div>
  );
}

function QuotaPeriod({ current, needed, locked, onSaved }: { current: string | null; needed: boolean; locked: boolean; onSaved: () => unknown }) {
  const [value, setValue] = useState(current ?? "");
  useEffect(() => setValue(current ?? ""), [current]);
  const save = useMutation({
    mutationFn: () => api.PUT("/api/v1/resource-groups/settings", { body: { cpu_quota_period: value.trim() || null } }).then(unwrap),
    onSuccess: onSaved,
  });
  return (
    <section className="card">
      <div className="chd">
        <span className="ct">CPU quota period</span>
        <span className="sp" />
        {needed && <Pill tone="a">Needed now</Pill>}
      </div>
      <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {needed && <span>A group sets a CPU limit. Trino will not start with a CPU limit and no quota period.</span>}
        <label className="fl">
          How often CPU limits reset
          <span style={{ display: "flex", gap: 8 }}>
            <input className="inp mono" placeholder="e.g. 1h" value={value} onChange={(e) => setValue(e.target.value)} />
            <button type="button" className="btn" disabled={locked || save.isPending || value === (current ?? "")} onClick={() => save.mutate()}>
              Stage
            </button>
          </span>
        </label>
        <ErrorNote error={save.error} />
      </div>
    </section>
  );
}

/**
 * The ordered selector list. Reordered locally and staged as a whole, because the order is
 * the meaning: the first selector that matches wins, so moving one is a change to which
 * group every query it and the ones it passed would have matched lands in.
 */
function SelectorList({
  selectors,
  error,
  before,
  groups,
  locked,
}: {
  selectors: Selector[] | null;
  error: unknown;
  before: unknown;
  groups: string[];
  locked: boolean;
}) {
  const invalidate = useInvalidateCandidate();
  const [draft, setDraft] = useState<Selector[] | null>(null);
  const [adding, setAdding] = useState(false);
  const list = draft ?? selectors ?? [];
  const save = useMutation({
    mutationFn: (next: Selector[]) => api.PUT("/api/v1/resource-groups/selectors", { body: { selectors: next } }).then(unwrap),
    onSuccess: async () => {
      await invalidate();
      setDraft(null);
    },
  });
  const move = (i: number, by: number) => {
    const next = [...list];
    const [s] = next.splice(i, 1);
    next.splice(i + by, 0, s!);
    setDraft(next);
  };
  const wasAt = new Map<string, number>();
  if (Array.isArray(before)) for (const m of selectorMoves(before, list)) if (m.from !== null && m.to !== null) wasAt.set(JSON.stringify(m.selector), m.from);

  return (
    <section className="card" style={{ flex: "999 1 560px", minWidth: 0 }}>
      <div className="chd">
        <span className="ct">Selectors</span>
        <span className="sm muted">checked top to bottom · the first match wins</span>
        <span className="sp" />
        <button type="button" className="btn pri" disabled={locked} onClick={() => setAdding(true)}>
          + Add selector
        </button>
      </div>
      <ErrorNote error={error ?? save.error} flush={false} />
      {selectors === null && !error ? (
        <div className="cp"><Loading what="the selectors" /></div>
      ) : list.length === 0 ? (
        <div className="cp muted">No selectors: every query goes to the root group.</div>
      ) : (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Target group</th>
                <th>Matches</th>
                <th aria-label="Order" />
              </tr>
            </thead>
            <tbody>
              {list.map((s, i) => {
                const was = wasAt.get(JSON.stringify(s));
                return (
                  <tr key={`${i}-${JSON.stringify(s)}`} className={was !== undefined ? "stg" : undefined}>
                    <td className="mono">
                      {i + 1}
                      {was !== undefined && <span className="sm muted"> was {was + 1}</span>}
                    </td>
                    <td>
                      <span className="nm">{s.group}</span> {was !== undefined && <Pill tone="b">Moved</Pill>}
                    </td>
                    <td className={describeSelector(s).startsWith("No conditions") ? "muted" : "mono"} style={{ padding: "8px 14px" }}>
                      {describeSelector(s)}
                    </td>
                    <td className="right" style={{ whiteSpace: "nowrap" }}>
                      <button type="button" className="btn btn--sm" aria-label={`Move selector ${i + 1} up`} disabled={locked || i === 0} onClick={() => move(i, -1)}>
                        ↑
                      </button>{" "}
                      <button type="button" className="btn btn--sm" aria-label={`Move selector ${i + 1} down`} disabled={locked || i === list.length - 1} onClick={() => move(i, 1)}>
                        ↓
                      </button>{" "}
                      <button type="button" className="btn btn--sm" aria-label={`Remove selector ${i + 1}`} disabled={locked} onClick={() => setDraft(list.filter((_, j) => j !== i))}>
                        ×
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {draft !== null && (
        <div className="cft">
          <span className="sm">The order changed here and is not staged yet.</span>
          <span className="sp" />
          <button type="button" className="btn" onClick={() => setDraft(null)}>
            Discard
          </button>
          <button type="button" className="btn pri" disabled={save.isPending} onClick={() => save.mutate(draft)}>
            Stage the order
          </button>
        </div>
      )}
      {wasAt.size > 0 && draft === null && (
        <div className="note" style={{ marginTop: 12 }}>
          The staged order differs from the Snapshot's. A query matching a moved selector and one it passed now lands in the moved
          selector's group.
        </div>
      )}
      <SelectorDialog
        open={adding}
        groups={groups}
        onClose={() => setAdding(false)}
        onAdd={(s) => {
          setAdding(false);
          save.mutate([...list.slice(0, -1), s, ...list.slice(-1)].filter(Boolean) as Selector[]);
        }}
      />
    </section>
  );
}

const CONDITIONS: Array<[keyof Selector, string]> = [
  ["user", "User (regex)"],
  ["user_group", "User group (regex)"],
  ["source", "Source (regex)"],
  ["query_text", "Query text (regex)"],
];

function SelectorDialog({ open, groups, onClose, onAdd }: { open: boolean; groups: string[]; onClose: () => void; onAdd: (s: Selector) => void }) {
  const [draft, setDraft] = useState<Selector>({ group: "" });
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add a selector"
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn pri" disabled={draft.group === ""} onClick={() => onAdd(Object.fromEntries(Object.entries(draft).filter(([, v]) => v !== "" && v != null)) as Selector)}>
            Stage
          </button>
        </>
      }
    >
      <label className="fl">
        Target group
        <select className="inp mono" value={draft.group} onChange={(e) => setDraft({ ...draft, group: e.target.value })}>
          <option value="">Choose…</option>
          {groups.map((g) => (
            <option key={g}>{g}</option>
          ))}
        </select>
      </label>
      {CONDITIONS.map(([k, label]) => (
        <label className="fl" key={k}>
          {label}
          <input className="inp mono" value={(draft[k] as string | undefined) ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
        </label>
      ))}
      <span className="hint">Added just above the last selector, so a catch-all at the bottom keeps catching what is left.</span>
    </Dialog>
  );
}

const NUM: Array<[keyof S["ResourceGroupWrite"], string]> = [
  ["hard_concurrency_limit", "Hard concurrency limit"],
  ["soft_concurrency_limit", "Soft concurrency limit"],
  ["max_queued", "Max queued"],
  ["scheduling_weight", "Scheduling weight"],
];
const TEXT: Array<[keyof S["ResourceGroupWrite"], string]> = [
  ["soft_memory_limit", "Soft memory limit (e.g. 30% or 50GB)"],
  ["soft_cpu_limit", "Soft CPU limit (e.g. 1h)"],
  ["hard_cpu_limit", "Hard CPU limit (e.g. 4h)"],
];

function GroupDialog({ group, onClose }: { group: Group | "new" | null; onClose: () => void }) {
  const invalidate = useInvalidateCandidate();
  const isNew = group === "new";
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [loaded, setLoaded] = useState<string | null>(null);
  const id = group === null ? null : isNew ? "#new" : group.path;
  if (id !== loaded) {
    setLoaded(id);
    setDraft(group === null || isNew ? { path: "", hard_concurrency_limit: 10 } : { ...group });
  }
  const body = () => {
    const out: Record<string, unknown> = {};
    for (const [k] of [...NUM, ...TEXT]) {
      const v = draft[k];
      out[k] = v === "" || v === undefined ? null : v;
    }
    out.hard_concurrency_limit = Number(draft.hard_concurrency_limit);
    out.scheduling_policy = draft.scheduling_policy || null;
    return out;
  };
  const save = useMutation({
    mutationFn: () =>
      isNew
        ? api.POST("/api/v1/resource-groups", { body: { ...body(), path: String(draft.path) } as S["ResourceGroupCreate"] }).then(unwrap)
        : api.PUT("/api/v1/resource-groups/{path}", { params: { path: { path: (group as Group).path } }, body: body() as S["ResourceGroupWrite"] }).then(unwrap),
    onSuccess: async () => {
      await invalidate();
      save.reset();
      onClose();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/resource-groups/{path}", { params: { path: { path: (group as Group).path } } }).then(done),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
  });
  return (
    <Dialog
      open={group !== null}
      onClose={onClose}
      title={isNew ? "Add a group" : `Edit ${group === null ? "" : (group as Group).path}`}
      actions={
        <>
          {!isNew && (
            <button type="button" className="btn dng" style={{ marginRight: "auto" }} disabled={remove.isPending} onClick={() => remove.mutate()}>
              Remove group
            </button>
          )}
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn pri" disabled={save.isPending} onClick={() => save.mutate()}>
            Stage
          </button>
        </>
      }
    >
      {isNew && (
        <label className="fl">
          Path <span className="hint">dotted, under an existing group: global.etl.batch</span>
          <input className="inp mono" value={String(draft.path ?? "")} onChange={(e) => setDraft({ ...draft, path: e.target.value })} />
        </label>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        {NUM.map(([k, label]) => (
          <label className="fl" key={k}>
            {label}
            <input className="inp mono" type="number" min={0} value={draft[k] == null ? "" : String(draft[k])} onChange={(e) => setDraft({ ...draft, [k]: e.target.value === "" ? null : Number(e.target.value) })} />
          </label>
        ))}
        {TEXT.map(([k, label]) => (
          <label className="fl" key={k}>
            {label}
            <input className="inp mono" value={draft[k] == null ? "" : String(draft[k])} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          </label>
        ))}
        <label className="fl">
          Scheduling policy
          <select className="inp" value={String(draft.scheduling_policy ?? "")} onChange={(e) => setDraft({ ...draft, scheduling_policy: e.target.value })}>
            <option value="">default</option>
            {["fair", "weighted", "weighted_fair", "query_priority"].map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>
      </div>
      <span className="hint">Read once at coordinator start: applying this restarts it and running queries will be terminated.</span>
      <ErrorNote error={save.error ?? remove.error} />
    </Dialog>
  );
}
