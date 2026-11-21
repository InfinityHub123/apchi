import { useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, done, postForm, unwrap, type S } from "../api/client";
import { useCertificates, useInvalidateCandidate } from "../api/queries";
import { SectionHead } from "../sections/SectionHead";
import { useSectionState } from "../sections/useSectionState";
import { Dialog } from "../ui/Dialog";
import { date, daysUntil, plural } from "../ui/format";
import { ChangePill, Pill } from "../ui/Pill";
import { ErrorNote, Loading } from "../ui/States";

const TOKEN = /\$\{(?:cert|key):([A-Za-z0-9][A-Za-z0-9._-]*)\}/g;

/** Which Catalogs present each certificate, by the field or by a `${cert:…}` token. */
function useUsedBy() {
  const catalogs = useQuery({
    queryKey: ["section", "catalogs"],
    queryFn: () => api.GET("/api/v1/catalogs").then(unwrap),
  });
  const users = new Map<string, string[]>();
  for (const c of catalogs.data ?? []) {
    const names = new Set<string>();
    if (c.certificate) names.add(c.certificate);
    for (const v of Object.values(c.properties ?? {})) for (const m of String(v).matchAll(TOKEN)) names.add(m[1]!);
    for (const n of names) users.set(n, [...(users.get(n) ?? []), c.name]);
  }
  return { users, ready: catalogs.data !== undefined };
}

const STATUS: Record<string, ["g" | "a" | "r", string]> = {
  valid: ["g", "Valid"],
  expiring: ["a", "Expiring"],
  expired: ["r", "Expired"],
};

/** Certificates Trino presents when it connects *out* to a database. */
export function CertificatesScreen() {
  const certs = useCertificates();
  const state = useSectionState("client_certificates");
  const used = useUsedBy();
  const [filter, setFilter] = useState("");
  const [remove, setRemove] = useState<string | null>(null);
  const list = certs.data ?? [];
  const window = list[0]?.expiring_within_days;
  const count = (s: string) => list.filter((c) => c.status === s).length;
  const unused = used.ready ? list.filter((c) => !used.users.has(c.name)).length : null;
  const rows = list.filter((c) => `${c.name} ${c.common_name ?? ""} ${c.issuer}`.toLowerCase().includes(filter.toLowerCase()));
  const removed = [...state.changes.values()].filter((c) => c.change === "removed");

  return (
    <div className="page">
      <SectionHead
        name="client_certificates"
        sub={<>Certificates Trino presents when it connects <b>out</b> to your databases</>}
        rollout={state.rollout}
        locked={state.locked}
      />
      <div className="row">
        <div style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
            <Stat label="Valid" n={count("valid")} />
            <Stat label={`Expiring within ${window ?? "…"} days`} n={count("expiring")} tone="a" />
            <Stat label="Expired" n={count("expired")} tone="r" />
            <Stat label="Unused" n={unused} />
          </div>
          <section className="card">
            <div className="chd">
              <input className="inp" style={{ maxWidth: 260 }} placeholder="Filter certificates" aria-label="Filter certificates" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <span className="sp" />
              <span className="sm muted">{plural(list.length, "certificate")}</span>
            </div>
            <ErrorNote error={certs.error} flush={false} />
            {certs.data === undefined && !certs.error ? (
              <div className="cp"><Loading what="the certificates" /></div>
            ) : list.length === 0 && removed.length === 0 ? (
              <div className="cp muted">No client certificates. Upload one to stage it.</div>
            ) : (
              <div className="tbl-wrap">
                <table className="nw">
                  <thead>
                    <tr>
                      <th>Certificate</th>
                      <th>Valid until</th>
                      <th>Used by</th>
                      <th>Status</th>
                      <th aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => {
                      const change = state.changes.get(c.name);
                      const [tone, label] = STATUS[c.status] ?? ["x", c.status];
                      const days = daysUntil(c.not_after);
                      const users = used.users.get(c.name) ?? [];
                      return (
                        <tr key={c.name} className={change ? "stg" : undefined}>
                          <td style={{ padding: "8px 14px" }}>
                            <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                              <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                                <b style={{ fontWeight: 600, color: "var(--ink)" }}>{c.name}</b>
                                {change && <ChangePill change={change.change} />}
                              </span>
                              <span className="mono sm muted">{c.common_name ?? c.subject}</span>
                            </span>
                          </td>
                          <td>
                            {date(c.not_after)} {days >= 0 && <span className="muted">· {plural(days, "day")}</span>}
                          </td>
                          <td className={users.length ? "mono" : "muted"}>{users.length ? users.join(", ") : used.ready ? "not used" : "…"}</td>
                          <td>
                            <Pill tone={tone} dot>
                              {label}
                            </Pill>
                          </td>
                          <td className="right">
                            <button type="button" className="btn btn--sm dng" disabled={state.locked} onClick={() => setRemove(c.name)}>
                              Remove
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                    {removed.map((c) => (
                      <tr key={c.resource} className="stg">
                        <td style={{ padding: "8px 14px" }}>
                          <b>{c.resource}</b> <ChangePill change="removed" />
                        </td>
                        <td colSpan={4} className="muted">
                          Removed from the Candidate. Use Reset in Review to keep it.
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
        <aside style={{ flex: "1 1 340px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          <Upload locked={state.locked} />
        </aside>
      </div>
      <RemoveDialog name={remove} users={remove === null ? [] : used.users.get(remove) ?? []} onClose={() => setRemove(null)} />
    </div>
  );
}

function Stat({ label, n, tone }: { label: string; n: number | null; tone?: "a" | "r" }) {
  return (
    <div className="card cp">
      <div className="lab">{label}</div>
      <div className="mono stat" style={tone && n ? { color: tone === "a" ? "var(--amber)" : "var(--red)" } : undefined}>
        {n ?? "…"}
      </div>
    </div>
  );
}

/**
 * Upload in two steps: read the archive, show what Apchi found, then stage. Reading refuses
 * for exactly the reasons staging would, so a confirmation that got this far will stage.
 */
function Upload({ locked }: { locked: boolean }) {
  const invalidate = useInvalidateCandidate();
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const form = () => {
    const f = new FormData();
    f.set("name", name.trim());
    f.set("archive", file!);
    return f;
  };
  const inspect = useMutation({ mutationFn: () => postForm<S["BundleReading"]>("/api/v1/certificates/inspect", form()) });
  const stage = useMutation({
    mutationFn: () => postForm<S["ClientCertificate"]>("/api/v1/certificates", form()),
    onSuccess: async () => {
      await invalidate();
      inspect.reset();
      setFile(null);
      setName("");
    },
  });
  const pick = (f: File | null) => {
    setFile(f);
    inspect.reset();
    stage.reset();
  };
  const reading = inspect.data;

  return (
    <>
      <section className="card">
        <div className="chd">
          <span className="ct">Upload certificate</span>
        </div>
        <form
          className="cp"
          style={{ display: "flex", flexDirection: "column", gap: 12 }}
          onSubmit={(e) => {
            e.preventDefault();
            inspect.mutate();
          }}
        >
          <label className="fl">
            Name
            <input className="inp mono" required value={name} onChange={(e) => setName(e.target.value)} placeholder="hr_pg_tls" />
            <span className="hint">An existing name is replaced — that is how renewal works. Catalogs keep pointing at the name.</span>
          </label>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pick(e.dataTransfer.files[0] ?? null);
            }}
            style={{
              border: `1px dashed ${dragging ? "var(--accent)" : "#cbd2db"}`,
              background: dragging ? "var(--accent-bg)" : "var(--panel-2)",
              borderRadius: 8,
              padding: 16,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 6,
              textAlign: "center",
            }}
          >
            <b>{file ? file.name : "Drop a ZIP here"}</b>
            <span className="sm muted">
              Certificate and private key, any common format. Apchi reads it first and shows you what it found. Nothing is staged until
              you confirm.
            </span>
            <input ref={input} type="file" accept=".zip,application/zip" hidden aria-label="Certificate archive" onChange={(e) => pick(e.target.files?.[0] ?? null)} />
            <button type="button" className="btn" onClick={() => input.current?.click()}>
              Choose file
            </button>
          </div>
          <button type="submit" className="btn pri" disabled={locked || file === null || name.trim() === "" || inspect.isPending}>
            {inspect.isPending ? "Reading…" : "Read the archive"}
          </button>
          <ErrorNote error={inspect.error} />
          {inspect.isError && <span className="sm muted">Reading refuses for exactly the reasons staging would.</span>}
        </form>
      </section>

      {reading !== undefined && (
        <section className="card" aria-label="What Apchi read">
          <div className="chd">
            <span className="ct">
              <span className="mono">{name}</span> — read, not staged
            </span>
            <span className="sp" />
            {reading.replaces ? <Pill tone="a">Would replace</Pill> : <Pill tone="b">New</Pill>}
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="kv" style={{ gridTemplateColumns: "auto 1fr" }}>
              <span className="k">Valid until</span>
              <span className="v">
                {reading.replaces && (
                  <>
                    <Pill tone={STATUS[reading.replaces.status]?.[0] ?? "x"}>{date(reading.replaces.not_after)}</Pill>{" "}
                    <span className="muted" aria-label="becomes">→</span>{" "}
                  </>
                )}
                <Pill tone={STATUS[reading.certificate.status]?.[0] ?? "x"}>{date(reading.certificate.not_after)}</Pill>
              </span>
              <span className="k">Common name</span>
              <span className="v mono">
                {reading.certificate.common_name ?? "—"}
                {reading.replaces && reading.replaces.common_name === reading.certificate.common_name && <span className="muted"> · unchanged</span>}
              </span>
              <span className="k">Issuer</span>
              <span className="v">
                {reading.certificate.issuer}
                {reading.replaces && reading.replaces.issuer === reading.certificate.issuer && <span className="muted"> · unchanged</span>}
              </span>
            </div>
            <div className="kv" style={{ gridTemplateColumns: "auto 1fr" }}>
              <span className="k">Certificate</span>
              <span className="v mono">{reading.certificate_member}</span>
              <span className="k">Private key</span>
              <span className="v mono">{reading.key_member}</span>
              {(reading.conversions ?? []).length > 0 && (
                <>
                  <span className="k">Converted</span>
                  <span className="v">{(reading.conversions ?? []).join(" ")}</span>
                </>
              )}
              {(reading.used_by ?? []).length > 0 && (
                <>
                  <span className="k">Re-points</span>
                  <span className="v">
                    <span className="mono">{(reading.used_by ?? []).join(", ")}</span>{" "}
                    <span className="sm muted">— every catalog presenting this name gets the new one</span>
                  </span>
                </>
              )}
            </div>
            <div className="sm muted">
              Apchi picks members by what they parse as, never by file name — check the certificate it picked is your leaf, not the issuing
              CA.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="button" className="btn pri" disabled={stage.isPending || locked} onClick={() => stage.mutate()}>
                {stage.isPending ? "Staging…" : reading.replaces ? "Stage the replacement" : "Stage the certificate"}
              </button>
              <button type="button" className="btn" onClick={() => inspect.reset()}>
                Cancel
              </button>
            </div>
            <ErrorNote error={stage.error} />
          </div>
        </section>
      )}
    </>
  );
}

function RemoveDialog({ name, users, onClose }: { name: string | null; users: string[]; onClose: () => void }) {
  const invalidate = useInvalidateCandidate();
  const remove = useMutation({
    mutationFn: () => api.DELETE("/api/v1/certificates/{name}", { params: { path: { name: name! } } }).then(done),
    onSuccess: async () => {
      await invalidate();
      onClose();
    },
  });
  return (
    <Dialog
      open={name !== null}
      onClose={onClose}
      title={`Remove ${name ?? ""}?`}
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
      {users.length > 0 ? (
        <p style={{ margin: 0 }}>
          <span className="mono">{users.join(", ")}</span> {users.length === 1 ? "presents" : "present"} this certificate. Apchi refuses to remove
          a certificate a Catalog still uses; point {users.length === 1 ? "it" : "them"} elsewhere first.
        </p>
      ) : (
        <p style={{ margin: 0 }}>No Catalog presents it. The removal is staged and reaches the cluster at the next Apply.</p>
      )}
      <ErrorNote error={remove.error} />
    </Dialog>
  );
}
