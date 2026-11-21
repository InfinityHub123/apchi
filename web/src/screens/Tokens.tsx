import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, done, unwrap, type S } from "../api/client";
import { LoadOlder, usePaged } from "../api/paged";
import { useIdentity } from "../auth/useAuth";
import { date, dayTime } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

const EXPIRY: Array<[number | null, string]> = [
  [90, "In 90 days"],
  [30, "In 30 days"],
  [365, "In 1 year"],
  [null, "Never — revoke it when it should stop"],
];

function status(t: S["Token"]): ["g" | "x" | "r", string] {
  if (t.revoked_at != null) return ["x", `Revoked ${dayTime(t.revoked_at)}`];
  if (t.expires_at != null && new Date(t.expires_at) < new Date()) return ["x", "Expired"];
  return ["g", "Live"];
}

/** Long-lived credentials for automation. A token acts as you, with your role. */
export function TokensScreen() {
  const me = useIdentity().data;
  const queries = useQueryClient();
  const paged = usePaged(["tokens"], (before) => api.GET("/api/v1/tokens", { params: { query: { limit: 50, before } } }).then(unwrap));
  const [name, setName] = useState("");
  const [days, setDays] = useState<number | null>(90);
  const [minted, setMinted] = useState<S["Minted"] | null>(null);
  const [copied, setCopied] = useState(false);
  const mint = useMutation({
    mutationFn: () => api.POST("/api/v1/tokens", { body: days === null ? { name } : { name, expires_in_days: days } }).then(unwrap),
    onSuccess: async (m) => {
      setMinted(m);
      setCopied(false);
      setName("");
      await queries.invalidateQueries({ queryKey: ["tokens"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.DELETE("/api/v1/tokens/{token_id}", { params: { path: { token_id: id } } }).then(done),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["tokens"] }),
  });

  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <PageHead title="API tokens" sub="Long-lived credentials for automation · a token acts as you, with your role, and loses access when you do · you see only your own" />
      {minted !== null && (
        <section className="card" style={{ borderColor: "var(--amber-bd)" }} aria-label="New token secret">
          <div className="chd" style={{ background: "var(--amber-bg)", borderColor: "var(--amber-bd)" }}>
            <span className="ct">Token “{minted.token.name}” created — copy it now</span>
            <span className="sp" />
            <Pill tone="a">Shown once</Pill>
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <code className="inp mono" style={{ display: "flex", alignItems: "center", overflowX: "auto", whiteSpace: "nowrap" }}>
                {minted.secret}
              </code>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  void navigator.clipboard?.writeText(minted.secret);
                  setCopied(true);
                }}
              >
                {copied ? "Copied" : "Copy"}
              </button>
              <button type="button" className="btn pri" onClick={() => setMinted(null)}>
                I have saved it
              </button>
            </div>
            <span className="sm muted">
              Apchi keeps only a digest. This secret cannot be shown again — a lost token is revoked and replaced. It stays on screen until
              you dismiss it.
            </span>
          </div>
        </section>
      )}
      <div className="row">
        <section className="card" style={{ flex: "999 1 560px", minWidth: 0 }}>
          <div className="chd">
            <span className="ct">Your tokens</span>
            <span className="sm muted">live, expired and revoked</span>
          </div>
          <ErrorNote error={paged.error ?? revoke.error} flush={false} />
          {!paged.loaded && !paged.error ? (
            <div className="cp"><Loading what="your tokens" /></div>
          ) : paged.items.length === 0 ? (
            <div className="cp muted">You have no tokens.</div>
          ) : (
            <div className="tbl-wrap">
              <table className="nw">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th>Expires</th>
                    <th>Status</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {paged.items.map((t) => {
                    const [tone, label] = status(t);
                    return (
                      <tr key={t.id}>
                        <td className="mono" style={{ fontWeight: 600 }}>{t.name}</td>
                        <td>{date(t.created_at)}</td>
                        <td>{t.last_used_at ? dayTime(t.last_used_at) : <span className="muted">never</span>}</td>
                        <td>{t.expires_at ? date(t.expires_at) : <span className="muted">never expires</span>}</td>
                        <td>
                          <Pill tone={tone}>{label}</Pill>
                        </td>
                        <td className="right">
                          {label === "Live" && (
                            <button type="button" className="btn btn--sm dng" disabled={revoke.isPending} onClick={() => revoke.mutate(t.id)}>
                              Revoke
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <LoadOlder paged={paged} />
        </section>
        <form
          className="card"
          style={{ flex: "1 1 300px", minWidth: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            mint.mutate();
          }}
        >
          <div className="chd">
            <span className="ct">Create a token</span>
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <label className="fl">
              Name
              <input className="inp mono" required value={name} onChange={(e) => setName(e.target.value)} placeholder="nightly-export" />
              <span className="hint">What it is for, so you can tell yours apart later</span>
            </label>
            <fieldset style={{ border: 0, margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              <legend className="fl" style={{ marginBottom: 6 }}>Expires</legend>
              {EXPIRY.map(([v, label]) => (
                <label key={String(v)} className="check">
                  <input type="radio" name="expiry" checked={days === v} onChange={() => setDays(v)} />
                  {label}
                </label>
              ))}
            </fieldset>
            <button type="submit" className="btn pri" disabled={mint.isPending || name.trim() === ""}>
              Create token
            </button>
            <ErrorNote error={mint.error} />
            {me !== undefined && (
              <span className="hint">
                It will act as {me.subject} ({me.role === "admin" ? "Admin" : "Operator"}). Nobody can create a token more powerful than
                themselves.
              </span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
