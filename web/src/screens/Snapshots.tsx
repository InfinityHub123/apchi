import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, unwrap, type SectionName } from "../api/client";
import { LoadOlder, usePaged } from "../api/paged";
import { useCluster, useInvalidateCandidate } from "../api/queries";
import { Effect } from "../sections/RevertSection";
import { SECTIONS, section } from "../sections";
import { Dialog } from "../ui/Dialog";
import { dateTime, dayTime } from "../ui/format";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";
import { download } from "../ui/download";

type RevertPath = `/api/v1/${string}/revert`;

/** Every configuration that was applied and verified, newest first. */
export function SnapshotsScreen() {
  const [params, setParams] = useSearchParams();
  const live = useCluster().data?.snapshot ?? null;
  const paged = usePaged(["snapshots", "all"], (before) => api.GET("/api/v1/snapshots", { params: { query: { limit: 20, before } } }).then(unwrap));
  // Whether each Snapshot's Apply restarted Trino: read from the Applies, which is where it
  // is recorded, one page of them being far more Applies than Snapshots anyone pages through.
  const applies = useQuery({ queryKey: ["applies", "for-snapshots"], queryFn: () => api.GET("/api/v1/applies", { params: { query: { limit: 200 } } }).then(unwrap) });
  const rolled = new Map((applies.data?.items ?? []).map((a) => [a.id, a.rolled_out]));
  const selected = Number(params.get("n") ?? paged.items[0]?.number ?? NaN);

  return (
    <div className="page">
      <PageHead title="Snapshots" sub="Every configuration that was applied and verified, newest first">
        <Link className="btn" to="/import-export">
          Import / export
        </Link>
      </PageHead>
      <ErrorNote error={paged.error} />
      {!paged.loaded && !paged.error && <Loading what="the Snapshots" />}
      {paged.loaded && paged.items.length === 0 && <div className="card cp muted">No Snapshot yet: the first one is created by the first Apply that verifies.</div>}
      {paged.items.length > 0 && (
        <div className="row">
          <section className="card" style={{ flex: "1 1 380px", minWidth: 0 }}>
            <div className="tbl-wrap">
              <table className="nw">
                <thead>
                  <tr>
                    <th>Snapshot</th>
                    <th>Committed</th>
                    <th>Behavior</th>
                  </tr>
                </thead>
                <tbody>
                  {paged.items.map((s) => (
                    <tr key={s.number} className={`click${s.number === selected ? " sel" : ""}`} onClick={() => setParams({ n: String(s.number) })} aria-selected={s.number === selected}>
                      <td className="mono" style={{ fontWeight: 600 }}>
                        {s.number} {s.number === live && <Pill tone="g">Live</Pill>}
                      </td>
                      <td>{dayTime(s.created_at)}</td>
                      <td>{rolled.has(s.apply_id) ? <CostPill rollout={rolled.get(s.apply_id)!} /> : <span className="muted">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <LoadOlder paged={paged} />
          </section>
          <div style={{ flex: "999 1 560px", minWidth: 0 }}>{Number.isFinite(selected) && <SnapshotDetail number={selected} live={live} />}</div>
        </div>
      )}
    </div>
  );
}

function SnapshotDetail({ number, live }: { number: number; live: number | null }) {
  const invalidate = useInvalidateCandidate();
  const snap = useQuery({
    queryKey: ["snapshot", number],
    queryFn: () => api.GET("/api/v1/snapshots/{number}", { params: { path: { number } } }).then(unwrap),
    staleTime: Infinity,
  });
  const [tab, setTab] = useState<SectionName>("catalogs");
  const [rollback, setRollback] = useState(false);
  const exporting = useMutation({ mutationFn: () => download(`/api/v1/snapshots/${number}/export`, `apchi-snapshot-${number}.json`) });
  const path = `/api/v1/${section(tab).api}/revert` as RevertPath;
  const call = (dry_run: boolean) => api.POST(path as "/api/v1/catalogs/revert", { body: { snapshot: number, dry_run } }).then(unwrap);
  const preview = useQuery({ queryKey: ["revert-preview", number, tab], queryFn: () => call(true), enabled: number !== live, retry: false, staleTime: 0, gcTime: 0 });
  const stage = useMutation({ mutationFn: () => call(false), onSuccess: invalidate });

  if (snap.error) return <ErrorNote error={snap.error} />;
  if (snap.data === undefined) return <Loading what={`Snapshot ${number}`} />;
  const s = snap.data;
  const sections = s.sections as Record<string, Record<string, unknown>>;
  const count = (name: SectionName) => Object.keys(sections[name] ?? {}).filter((k) => !k.startsWith("#")).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <section className="card">
        <div className="chd">
          <span className="ct">Snapshot {s.number}</span>
          <Pill tone="g">Verified</Pill>
          {s.number === live && <Pill tone="g">Live</Pill>}
          <span className="sp" />
          <button type="button" className="btn" disabled={exporting.isPending} onClick={() => exporting.mutate()}>
            Export
          </button>
          {s.number !== live && (
            <button type="button" className="btn dng" onClick={() => setRollback(true)}>
              Full Rollback…
            </button>
          )}
        </div>
        <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <span className="sm muted">
            Committed {dateTime(s.created_at)} · Apply{" "}
            <Link className="mono" to={`/applies/${s.apply_id}`}>
              {s.apply_id}
            </Link>
          </span>
          <div className="kv" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(160px, auto) )" }}>
            {SECTIONS.map((x) => (
              <span key={x.key}>
                <span className="k">{x.label}</span> <span className="v mono">{count(x.key)}</span>
              </span>
            ))}
          </div>
          <span className="sm muted">Holds real credentials: an export is a secret, handle it like one.</span>
          <ErrorNote error={exporting.error} />
        </div>
      </section>

      {s.number !== live && (
        <section className="card">
          <div className="chd">
            <span className="ct">Section Revert</span>
            <span className="sm muted">one Section back to Snapshot {s.number}, every other left where it is</span>
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <span role="tablist" style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {SECTIONS.map((x) => (
                <button key={x.key} type="button" role="tab" aria-selected={tab === x.key} className={`tc${tab === x.key ? " sel" : ""}`} onClick={() => { setTab(x.key); stage.reset(); }}>
                  {x.short}
                </button>
              ))}
            </span>
            {preview.isFetching && <Loading what="Apchi's dry run" />}
            <ErrorNote error={preview.error ?? stage.error} />
            {preview.data !== undefined && !preview.isFetching && <Effect effect={preview.data} sectionLabel={section(tab).label} />}
            {stage.isSuccess ? (
              <div className="note note--blue note--flush">
                Staged. <Link to="/review">Review the Candidate</Link> before an Apply.
              </div>
            ) : (
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button type="button" className="btn pri" disabled={preview.data === undefined || stage.isPending} onClick={() => stage.mutate()}>
                  Stage revert
                </button>
              </div>
            )}
          </div>
        </section>
      )}
      <FullRollback number={s.number} open={rollback} onClose={() => setRollback(false)} />
    </div>
  );
}

/**
 * Full Rollback: every Section back to one Snapshot at once. Reserved for a serious mistake,
 * so the dry run is not optional here -- it is the only description of what discarding every
 * staged change in every Section, everyone's, amounts to.
 */
function FullRollback({ number, open, onClose }: { number: number; open: boolean; onClose: () => void }) {
  const invalidate = useInvalidateCandidate();
  const call = (dry_run: boolean) => api.POST("/api/v1/candidate/rollback", { body: { snapshot: number, dry_run } }).then(unwrap);
  const preview = useQuery({ queryKey: ["rollback-preview", number], queryFn: () => call(true), enabled: open, retry: false, gcTime: 0 });
  const stage = useMutation({
    mutationFn: () => call(false),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
  });
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Full Rollback to Snapshot ${number}`}
      actions={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn dng-solid" disabled={preview.data === undefined || stage.isPending} onClick={() => stage.mutate()}>
            Stage the Full Rollback
          </button>
        </>
      }
    >
      <div className="note note--red note--flush">
        Every Section is replaced, and <b>every staged change in the Candidate is discarded — including other people's</b>. It stages; an
        ordinary Apply follows. Admin values are not part of a Snapshot and are not restored.
      </div>
      {preview.isFetching && <Loading what="Apchi's dry run" />}
      {preview.data !== undefined && <Effect effect={preview.data} />}
      <ErrorNote error={preview.error ?? stage.error} />
    </Dialog>
  );
}
