import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, api, done, unwrap, type S } from "../api/client";
import { useInvalidateCandidate } from "../api/queries";
import { useIdentity } from "../auth/useAuth";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState } from "../sections/useSectionState";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading } from "../ui/States";

type Case = S["CertificateMapping"]["case"];

/**
 * What Trino would derive from a subject, computed in the browser.
 *
 * Anchored at both ends because Trino matches the whole subject. JavaScript's regular
 * expressions are close to Java's but not identical (possessive quantifiers, some character
 * classes), which is why the screen says Validation is the real check.
 */
export function derive(pattern: string, user: string, kase: Case, subject: string): { ok: true; value: string } | { ok: false; why: string } {
  let re: RegExp;
  try {
    re = new RegExp(`^(?:${pattern})$`);
  } catch {
    return { ok: false, why: "invalid pattern" };
  }
  const m = re.exec(subject);
  if (m === null) return { ok: false, why: "no match — falls through to preserved patterns" };
  let out = user.replace(/\$(\d)/g, (_, i: string) => m[Number(i)] ?? "");
  if (kase === "lower") out = out.toLowerCase();
  if (kase === "upper") out = out.toUpperCase();
  return { ok: true, value: out };
}

/** How someone connecting *in* with a certificate becomes a Trino Identity. */
export function CertMappingScreen() {
  const state = useSectionState("certificate_mapping");
  const admin = useIdentity().data?.role === "admin";
  const invalidate = useInvalidateCandidate();
  const current = useQuery({
    queryKey: ["section", "certificate_mapping"],
    queryFn: async () => {
      try {
        return await api.GET("/api/v1/certificate-mapping").then(unwrap);
      } catch (e) {
        // No pattern is an answer, not a failure: the Section is empty.
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
  });
  const [pattern, setPattern] = useState("");
  const [user, setUser] = useState("$1");
  const [kase, setKase] = useState<Case>("keep");
  const [subjects, setSubjects] = useState<string[]>(["CN=jo.levi, OU=analytics, O=Acme, C=GB", "CN=svc_etl_nightly, OU=analytics, O=Acme, C=GB"]);
  const [subject, setSubject] = useState("");

  useEffect(() => {
    if (current.data) {
      setPattern(current.data.pattern);
      setUser(current.data.user);
      setKase(current.data.case ?? "keep");
    }
  }, [current.data]);

  const save = useMutation({
    mutationFn: () => api.PUT("/api/v1/certificate-mapping", { body: { pattern, user, case: kase } }).then(unwrap),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/certificate-mapping").then(done),
    onSuccess: invalidate,
  });
  const dirty = current.data == null ? pattern !== "" : pattern !== current.data.pattern || user !== current.data.user || kase !== (current.data.case ?? "keep");

  return (
    <div className="page">
      <SectionHead
        name="certificate_mapping"
        sub={<>How someone connecting <b>in</b> to Trino with a certificate becomes a Trino Identity · one pattern for the whole cluster</>}
        rollout={true}
        locked={state.locked}
      />
      <div className="row">
        <div style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {state.changes.size > 0 && <div className="note note--blue note--flush">The pattern below is staged in the Candidate — not on the cluster yet.</div>}
          {current.data === undefined && !current.error ? (
            <Loading what="the pattern" />
          ) : (
            <section className="card">
              <div className="chd">
                <span className="ct">Pattern</span>
                <Pill tone="a">Read at coordinator start</Pill>
              </div>
              <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <ErrorNote error={current.error} />
                {current.data === null && pattern === "" && (
                  <span className="muted">No pattern. Certificate authentication derives no Trino Identity until one is staged.</span>
                )}
                <label className="fl">
                  Regular expression
                  <input className="inp mono" value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="CN=([^,]+), OU=analytics.*" />
                  <span className="hint">Matched against the whole certificate subject, as Trino renders it: most specific first, comma and space.</span>
                </label>
                <label className="fl">
                  Trino Identity to derive
                  <input className="inp mono" value={user} onChange={(e) => setUser(e.target.value)} />
                  <span className="hint">$1 is the first group in brackets</span>
                </label>
                <div className="fl" role="radiogroup" aria-label="Letter case">
                  Letter case
                  <span style={{ display: "flex", gap: 6 }}>
                    {(
                      [
                        ["keep", "Keep"],
                        ["lower", "lower"],
                        ["upper", "UPPER"],
                      ] as const
                    ).map(([v, label]) => (
                      <button key={v} type="button" role="radio" aria-checked={kase === v} className={`tc${kase === v ? " sel" : ""}`} onClick={() => setKase(v)}>
                        {label}
                      </button>
                    ))}
                  </span>
                </div>
              </div>
            </section>
          )}
          <section className="card">
            <div className="chd">
              <span className="ct">Try it</span>
              <span className="sm muted">certificate subject → Trino Identity</span>
            </div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Subject</th>
                    <th>Result</th>
                    <th aria-label="Remove" />
                  </tr>
                </thead>
                <tbody>
                  {subjects.map((s, i) => {
                    const r = pattern === "" ? { ok: false as const, why: "no pattern" } : derive(pattern, user, kase, s);
                    return (
                      <tr key={i}>
                        <td className="mono" style={{ wordBreak: "break-all" }}>{s}</td>
                        <td>{r.ok ? <span className="pill g mono">{r.value}</span> : <Pill tone={r.why === "invalid pattern" ? "r" : "x"}>{r.why}</Pill>}</td>
                        <td className="right">
                          <button type="button" className="btn btn--sm" aria-label={`Remove ${s}`} onClick={() => setSubjects((p) => p.filter((_, j) => j !== i))}>
                            ×
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <form
              className="cft"
              onSubmit={(e) => {
                e.preventDefault();
                if (subject.trim()) setSubjects((p) => [...p, subject.trim()]);
                setSubject("");
              }}
            >
              <input className="inp mono" style={{ flex: 1 }} aria-label="Subject to try" placeholder="CN=someone, OU=team, O=Acme" value={subject} onChange={(e) => setSubject(e.target.value)} />
              <button type="submit" className="btn">
                Try subject
              </button>
            </form>
            <div className="note" style={{ marginTop: 12 }}>
              Runs in your browser. Trino uses Java regular expressions, which differ in rare cases — Validation is the real check.
            </div>
          </section>
          <ErrorNote error={save.error ?? remove.error} />
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            <span className="sm muted">
              Saving stages a change that <b>restarts the coordinator</b> at the next Apply.
            </span>
            <span className="sp" />
            {current.data != null && (
              <button type="button" className="btn dng" disabled={state.locked || remove.isPending} onClick={() => remove.mutate()}>
                Remove pattern
              </button>
            )}
            <button type="button" className="btn pri" disabled={state.locked || !dirty || pattern === "" || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? "Staging…" : "Stage change"}
            </button>
          </div>
        </div>
        <aside style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {admin && <Preserved />}
          <section className="card cp sm muted">
            Not the same as <Link to="/certificates">Client certificates</Link> — those are what Trino presents when it connects out.
          </section>
        </aside>
      </div>
    </div>
  );
}

/** The Admin's preserved patterns: before Apchi, kept while clients migrate. */
function Preserved() {
  const queries = useQueryClient();
  const read = useQuery({
    queryKey: ["admin", "preserved-mappings"],
    queryFn: () => api.GET("/api/v1/admin/certificate-mapping/preserved").then(unwrap),
  });
  const [editing, setEditing] = useState<S["CertificateMappingWrite"][] | null>(null);
  const save = useMutation({
    mutationFn: (patterns: S["CertificateMappingWrite"][]) =>
      api.PUT("/api/v1/admin/certificate-mapping/preserved", { body: { patterns } }).then(unwrap),
    onSuccess: async () => {
      await queries.invalidateQueries({ queryKey: ["admin"] });
      setEditing(null);
    },
  });
  const patterns = read.data?.patterns ?? [];
  return (
    <section className="card">
      <div className="chd">
        <span className="ct">Preserved patterns</span>
        <span className="sp" />
        <Pill tone="x">Admin</Pill>
      </div>
      <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <span className="sm muted">Patterns from before Apchi, kept while clients migrate. Checked after yours, in this order.</span>
        <ErrorNote error={read.error ?? save.error} />
        {editing === null ? (
          <>
            {patterns.length === 0 && <span className="muted">None.</span>}
            {patterns.map((p, i) => (
              <div key={i} className="mono" style={{ display: "flex", gap: 8, fontSize: 12, wordBreak: "break-all" }}>
                <span className="muted">{i + 1}</span>
                {p.pattern} → {p.user}
              </div>
            ))}
            <button type="button" className="btn" style={{ alignSelf: "flex-start" }} onClick={() => setEditing(patterns.map((p) => ({ ...p })))}>
              Edit preserved patterns
            </button>
          </>
        ) : (
          <>
            {editing.map((p, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <span className="muted mono">{i + 1}</span>
                <input className="inp mono" aria-label={`Preserved pattern ${i + 1}`} value={p.pattern} onChange={(e) => setEditing(editing.map((x, j) => (j === i ? { ...x, pattern: e.target.value } : x)))} />
                <input className="inp mono" style={{ width: 70 }} aria-label={`Identity for pattern ${i + 1}`} value={p.user} onChange={(e) => setEditing(editing.map((x, j) => (j === i ? { ...x, user: e.target.value } : x)))} />
                <button type="button" className="btn btn--sm" aria-label={`Remove pattern ${i + 1}`} onClick={() => setEditing(editing.filter((_, j) => j !== i))}>
                  ×
                </button>
              </div>
            ))}
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" className="btn" onClick={() => setEditing([...editing, { pattern: "", user: "$1", case: "keep" }])}>
                + Add
              </button>
              <span className="sp" />
              <button type="button" className="btn" onClick={() => setEditing(null)}>
                Cancel
              </button>
              <button type="button" className="btn pri" disabled={save.isPending} onClick={() => save.mutate(editing)}>
                Save
              </button>
            </div>
            <span className="hint">An Admin value: saved now, never staged, delivered by the next Apply.</span>
          </>
        )}
      </div>
    </section>
  );
}
