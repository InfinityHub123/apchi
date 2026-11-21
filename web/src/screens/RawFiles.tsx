import { useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { sendRawFile, useRawFiles } from "../admin/requests";
import { bytes, dayTime } from "../ui/format";
import { ErrorNote, Loading, PageHead } from "../ui/States";

/**
 * The escape hatch: a whole Trino configuration file at a path the Admin chooses. Content is
 * write-only -- the API lists paths and sizes, never what is in them, because Apchi cannot
 * tell which part of a file is a credential.
 */
export function RawFilesScreen() {
  const files = useRawFiles();
  const queries = useQueryClient();
  // When each file was last written, from the audit: the listing carries no time, and the
  // trail is where an Admin change is recorded.
  const written = useQueries({
    queries: (files.data ?? []).map((f) => ({
      queryKey: ["audit", "raw-file", f.path],
      queryFn: () => api.GET("/api/v1/audit", { params: { query: { category: "admin", resource: f.path, limit: 1 } } }).then(unwrap),
    })),
  });
  const [path, setPath] = useState("/etc/trino/");
  const [content, setContent] = useState("");
  const refresh = () => queries.invalidateQueries({ queryKey: ["admin"] });
  const write = useMutation({
    mutationFn: () => sendRawFile("PUT", path.trim(), content),
    onSuccess: async () => {
      await refresh();
      setContent("");
    },
  });
  const remove = useMutation({ mutationFn: (p: string) => sendRawFile("DELETE", p), onSuccess: refresh });

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <PageHead
        crumb={<Link to="/admin">Admin values</Link>}
        title="Raw files"
        sub="The escape hatch: a whole Trino configuration file at a path you choose, delivered exactly as written"
      />
      <div className="row">
        <div style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          <section className="card">
            <div className="chd">
              <span className="ct">Files</span>
              <span className="sm muted">content is never shown again</span>
            </div>
            <ErrorNote error={files.error ?? remove.error} flush={false} />
            {files.data === undefined && !files.error ? (
              <div className="cp"><Loading what="the raw files" /></div>
            ) : files.data?.length === 0 ? (
              <div className="cp muted">No raw files.</div>
            ) : (
              <div className="tbl-wrap">
                <table className="nw">
                  <thead>
                    <tr>
                      <th>Path</th>
                      <th>Size</th>
                      <th>Last written</th>
                      <th aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {(files.data ?? []).map((f, i) => {
                      const event = written[i]?.data?.items[0];
                      return (
                        <tr key={f.path}>
                          <td className="mono">{f.path}</td>
                          <td className="mono">{bytes(f.bytes)}</td>
                          <td>{event ? `${dayTime(event.at)} by ${event.actor}` : <span className="muted">—</span>}</td>
                          <td className="right">
                            <button type="button" className="btn btn--sm" onClick={() => setPath(f.path)}>
                              Replace…
                            </button>{" "}
                            <button type="button" className="btn btn--sm dng" disabled={remove.isPending} onClick={() => remove.mutate(f.path)}>
                              Remove
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <div className="note" style={{ marginTop: 12 }}>
              The listing shows paths and sizes only. Apchi can't tell which part of a file is a credential, so it treats all of it as
              secret: stored in a Secret, never logged, never returned. To change a file, write it again in full.
            </div>
          </section>
          <section className="card">
            <div className="chd">
              <span className="ct">How a raw file reaches the cluster</span>
            </div>
            <div className="cp kv" style={{ gridTemplateColumns: "auto 1fr", alignItems: "start" }}>
              <span className="pill b">1</span>
              <span>Written here — nothing changes on the cluster yet.</span>
              <span className="pill x">2</span>
              <span>The next Apply (an Admin Apply, or an Operator's) validates it on a throwaway coordinator. If Trino rejects it, Validation fails and nothing is applied.</span>
              <span className="pill x">3</span>
              <span>Delivered, with a Rollout. If the cluster fails anyway, Auto Rollback restores the previous files.</span>
            </div>
          </section>
        </div>
        <form
          className="card"
          style={{ flex: "1 1 380px", minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            write.mutate();
          }}
        >
          <div className="chd">
            <span className="ct">Write a file</span>
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <label className="fl">
              Path
              <input className="inp mono" required value={path} onChange={(e) => setPath(e.target.value)} />
              <span className="hint">Must be under /etc/trino. Writing an existing path replaces it.</span>
            </label>
            <label className="fl">
              Content
              <textarea className="inp mono" required style={{ minHeight: 180 }} value={content} onChange={(e) => setContent(e.target.value)} />
              <span className="hint">Not parsed or checked by Apchi — Trino decides at Validation.</span>
            </label>
            <button type="submit" className="btn pri" disabled={write.isPending}>
              Write file
            </button>
            {write.isSuccess && <div className="note note--blue note--flush">Written. Nothing changes on the cluster until the next Apply.</div>}
            <ErrorNote error={write.error} />
            <div className="sm muted">
              <b>Refused when written:</b> a path Apchi generates (replacing it could silently open the cluster), and any path on a
              whole-directory deployment, where Kubernetes cannot place a single file. <span className="mono">config.properties</span> is allowed
              but not validated — it is proved on the cluster, with Auto Rollback as the safety net.
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
