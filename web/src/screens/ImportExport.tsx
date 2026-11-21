import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { useCluster, useInvalidateCandidate, useReview, useSnapshots } from "../api/queries";
import { useIdentity } from "../auth/useAuth";
import { candidateCounts } from "../review/counts";
import { SECTIONS } from "../sections";
import { plural } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, PageHead } from "../ui/States";
import { download } from "../ui/download";

type Source = { kind: "candidate" } | { kind: "snapshot"; n: number } | { kind: "admin" };

/** Move a configuration between clusters as one portable document. */
export function ImportExportScreen() {
  const admin = useIdentity().data?.role === "admin";
  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <PageHead title="Import / export" sub="Move a configuration between clusters as one portable document" />
      <div className="row">
        <Export admin={admin} />
        <Import admin={admin} />
      </div>
    </div>
  );
}

function Export({ admin }: { admin: boolean }) {
  const review = useReview().data;
  const live = useCluster().data?.snapshot ?? null;
  const snapshots = useSnapshots(10).data?.items ?? [];
  const [source, setSource] = useState<Source>({ kind: "candidate" });
  const run = useMutation({
    mutationFn: () =>
      source.kind === "candidate"
        ? download("/api/v1/candidate/export", "apchi-candidate.json")
        : source.kind === "snapshot"
          ? download(`/api/v1/snapshots/${source.n}/export`, `apchi-snapshot-${source.n}.json`)
          : download("/api/v1/admin/configuration/export", "apchi-configuration-with-admin-values.json"),
  });
  const staged = review === undefined ? null : candidateCounts(review).total;
  return (
    <section className="card" style={{ flex: "1 1 420px", minWidth: 0 }}>
      <div className="chd">
        <span className="ct">Export</span>
      </div>
      <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label className="check" style={{ alignItems: "flex-start" }}>
          <input type="radio" name="source" checked={source.kind === "candidate"} onChange={() => setSource({ kind: "candidate" })} />
          <span>
            <b>The Candidate</b>
            <br />
            <span className="sm muted">
              {live == null ? "Nothing applied yet" : `Snapshot ${live}`}
              {staged !== null && ` plus the ${plural(staged, "staged change")}`} — the only way to move work that was never applied
            </span>
          </span>
        </label>
        <label className="check" style={{ alignItems: "flex-start" }}>
          <input type="radio" name="source" checked={source.kind === "snapshot"} disabled={snapshots.length === 0} onChange={() => setSource({ kind: "snapshot", n: snapshots[0]!.number })} />
          <span>
            <b>A Snapshot</b>
            <br />
            <span className="sm muted">Any applied and verified configuration</span>
            {source.kind === "snapshot" && (
              <span style={{ display: "flex", gap: 4, marginTop: 6, flexWrap: "wrap" }}>
                {snapshots.map((s) => (
                  <button key={s.number} type="button" className={`tc${source.n === s.number ? " sel" : ""}`} onClick={() => setSource({ kind: "snapshot", n: s.number })}>
                    {s.number}
                  </button>
                ))}
              </span>
            )}
          </span>
        </label>
        {admin && (
          <label className="check" style={{ alignItems: "flex-start" }}>
            <input type="radio" name="source" checked={source.kind === "admin"} onChange={() => setSource({ kind: "admin" })} />
            <span>
              <b>Everything, with Admin values</b> <Pill tone="x">Admin</Pill>
              <br />
              <span className="sm muted">The six Sections plus enforcement, preserved rules and patterns, and raw files</span>
            </span>
          </label>
        )}
        <div className="note note--red note--flush">The document contains real secrets — database passwords, private keys. Handle it like a credential.</div>
        <button type="button" className="btn pri" style={{ alignSelf: "flex-start" }} disabled={run.isPending} onClick={() => run.mutate()}>
          Download JSON
        </button>
        <ErrorNote error={run.error} />
      </div>
    </section>
  );
}

function Import({ admin }: { admin: boolean }) {
  const review = useReview().data;
  const invalidate = useInvalidateCandidate();
  const [doc, setDoc] = useState<Record<string, unknown> | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [understood, setUnderstood] = useState(false);
  const withAdmin = doc !== null && doc.admin != null;
  const run = useMutation({
    mutationFn: async (): Promise<void> => {
      if (withAdmin) await api.PUT("/api/v1/admin/configuration", { body: doc as never }).then(unwrap);
      else await api.PUT("/api/v1/candidate", { body: doc as never }).then(unwrap);
    },
    onSuccess: async () => {
      await invalidate();
      setDoc(null);
      setFileName(null);
      setUnderstood(false);
    },
  });
  const read = async (f: File | undefined) => {
    run.reset();
    setParseError(null);
    setDoc(null);
    if (f === undefined) return;
    setFileName(f.name);
    try {
      setDoc(JSON.parse(await f.text()) as Record<string, unknown>);
    } catch {
      setParseError(`${f.name} is not JSON. An Apchi export is a single JSON document.`);
    }
  };
  const sections = (doc?.sections ?? {}) as Record<string, unknown>;
  const missing = doc === null ? [] : SECTIONS.filter((s) => !(s.key in sections));
  const total = review === undefined ? 0 : candidateCounts(review).total;

  return (
    <section className="card" style={{ flex: "1 1 460px", minWidth: 0 }}>
      <div className="chd">
        <span className="ct">Import</span>
        <span className="sm muted">Replaces the entire Candidate</span>
      </div>
      <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label className="fl">
          Exported document
          <input className="inp" type="file" accept="application/json,.json" onChange={(e) => void read(e.target.files?.[0])} />
        </label>
        {parseError !== null && <div className="note note--red note--flush">{parseError}</div>}
        <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 6 }} className="sm">
          <li>A document says what the configuration is, not what to change. <b>A Section missing from it becomes empty</b> — it is not left alone.</li>
          <li>
            <b>Everyone's staged work is replaced</b>, not just yours{total > 0 ? `: ${plural(total, "change")} now` : ""}.
          </li>
          <li>Not the same as Reset. Reset returns to the Snapshot and leaves nothing staged; an import is expected to stage a lot.</li>
          <li>Rejected whole if anything is wrong — every problem named, nothing stored.</li>
        </ul>
        {doc !== null && (
          <div className="note note--flush">
            <b className="mono">{fileName}</b> · {Object.keys(sections).length} of 6 Sections
            {withAdmin && (admin ? " · with Admin values, imported through the Admin route" : "")}
            {missing.length > 0 && (
              <div style={{ color: "var(--red)", marginTop: 6 }}>
                Missing, so they would be emptied: {missing.map((s) => s.label).join(", ")}. Include a Section, even empty, to say so on purpose.
              </div>
            )}
          </div>
        )}
        {withAdmin && !admin && (
          <div className="note note--red note--flush">This document carries Admin values. Only an Admin can import it; an Operator import would refuse it rather than drop them silently.</div>
        )}
        <label className="check">
          <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />I understand this replaces everything staged
        </label>
        <button type="button" className="btn dng-solid" style={{ alignSelf: "flex-start" }} disabled={doc === null || !understood || run.isPending || (withAdmin && !admin)} onClick={() => run.mutate()}>
          {run.isPending ? "Importing…" : "Import"}
        </button>
        <ErrorNote error={run.error} />
        {run.isSuccess && (
          <div className="note note--blue note--flush">
            Imported into the Candidate. Nothing reached the cluster. <Link to="/review">Review what an Apply would change</Link>.
          </div>
        )}
      </div>
    </section>
  );
}
