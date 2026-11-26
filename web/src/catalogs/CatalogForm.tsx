import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, api, unwrap, type S } from "../api/client";
import { useCertificates, useInvalidateCandidate } from "../api/queries";
import { ErrorNote } from "../ui/States";
import { WIRES_CERTIFICATE } from "./connectors";

type Rules = S["PropertyRules"];

/**
 * A Catalog's settings, laid out by its connector's rules.
 *
 * Required properties are fields. A branch selector (Iceberg's catalog type, Elasticsearch's
 * security) is a choice that decides which further properties are required; where the API
 * says any value is allowed (`allowed: null`) it is free entry with the known values offered,
 * not a closed list. Optional, open-ended and prefixed properties are added on demand, so the
 * form shows only what this Catalog uses. A connector with no rules is pass-through: name and
 * value pairs, checked by Trino at Validation.
 */
export type PropertyResource = {
  name: string;
  properties?: Record<string, string>;
  certificate?: string | null;
};

export function CatalogForm({
  rules,
  connector,
  initial,
  onStaged,
  onCancel,
}: {
  rules: Rules | null;
  connector: string;
  initial?: PropertyResource;
  onStaged: (name: string) => void;
  onCancel: () => void;
}) {
  const editing = initial !== undefined;
  const certificates = useCertificates();
  const invalidate = useInvalidateCandidate();
  const [name, setName] = useState(initial?.name ?? "");
  const [customConnector, setCustomConnector] = useState(rules === null && !editing ? "" : connector);
  const [values, setValues] = useState<Record<string, string>>(() => ({ ...(initial?.properties ?? {}) }));
  const [extra, setExtra] = useState<string[]>(() => {
    if (initial === undefined || rules === null) return rules === null ? Object.keys(initial?.properties ?? {}) : [];
    const known = new Set([...rules.required, ...rules.branches.map((b) => b.selector), ...rules.branches.flatMap((b) => Object.values(b.values).flat())]);
    return Object.keys(initial.properties ?? {}).filter((k) => !known.has(k));
  });
  const [certificate, setCertificate] = useState<string>(initial?.certificate ?? "");
  const [newKey, setNewKey] = useState("");

  const set = (k: string, v: string) => setValues((prev) => ({ ...prev, [k]: v }));
  const secret = (k: string) => rules?.secrets.includes(k) ?? /password|secret|token|credential/i.test(k);

  // The properties the current branch choices make required, in order.
  const branchFields = useMemo(() => {
    if (rules === null) return [];
    return rules.branches.flatMap((b) => {
      const chosen = values[b.selector] ?? b.default ?? "";
      return b.values[chosen] ?? [];
    });
  }, [rules, values]);

  const shown = rules === null ? extra : [...new Set([...rules.required, ...branchFields, ...extra])];
  const selectors = new Set(rules?.branches.map((b) => b.selector) ?? []);
  const addable =
    rules === null
      ? []
      : [...new Set([...rules.optional, ...rules.open_ended])].filter((k) => !shown.includes(k) && !selectors.has(k));

  const body = () => {
    const properties: Record<string, string> = {};
    if (rules !== null) {
      for (const b of rules.branches) {
        const v = values[b.selector];
        if (v !== undefined && v !== "") properties[b.selector] = v;
      }
    }
    for (const k of shown) {
      const v = values[k];
      if (v !== undefined && v !== "") properties[k] = v;
    }
    return properties;
  };

  const stage = useMutation({
    mutationFn: async () => {
      const properties = body();
      const cert = certificate === "" ? null : certificate;
      if (editing) {
        await api
          .PATCH("/api/v1/catalogs/{name}", {
            params: { path: { name: initial.name } },
            body: { properties, certificate: cert, connector: rules === null ? customConnector : undefined },
          })
          .then(unwrap);
        return initial.name;
      }
      await api
        .POST("/api/v1/catalogs", {
          body: { name, connector: rules === null ? customConnector : connector, properties, certificate: cert },
        })
        .then(unwrap);
      return name;
    },
    onSuccess: async (staged) => {
      await invalidate();
      onStaged(staged);
    },
  });

  const problems = new Map<string, string>();
  if (stage.error instanceof ApiError) {
    for (const d of stage.error.details) {
      if (typeof d.property === "string") problems.set(d.property, String(d.problem ?? d.message ?? ""));
    }
  }

  const preview = JSON.stringify(
    {
      ...(editing ? {} : { name: name || "…", connector: rules === null ? customConnector || "…" : connector }),
      properties: Object.fromEntries(Object.entries(body()).map(([k, v]) => [k, secret(k) ? "••••••••" : v])),
      ...(certificate !== "" ? { certificate } : {}),
    },
    null,
    2,
  );

  return (
    <div className="row">
      <form
        style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}
        onSubmit={(e) => {
          e.preventDefault();
          stage.mutate();
        }}
      >
        <section className="card">
          <div className="chd">
            <span className="ct">Catalog</span>
          </div>
          <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {!editing && (
              <label className="fl">
                Name
                <input
                  className="inp mono"
                  required
                  pattern="[a-z][a-z0-9_]*"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="finance_pg"
                />
                <span className="hint">
                  Lowercase letters, digits and _ · queried as <span className="mono">{name || "name"}.schema.table</span> ·
                  can't be renamed later (a rename is a drop and a create)
                </span>
              </label>
            )}
            {rules === null && (
              <label className="fl">
                Connector name
                <input className="inp mono" required value={customConnector} onChange={(e) => setCustomConnector(e.target.value)} placeholder="e.g. clickhouse" />
                <span className="hint">
                  A connector installed on your Trino. A near miss of a known name is caught (“did you mean postgresql?”).
                </span>
              </label>
            )}
            {rules?.branches.map((b) => (
              <Branch key={b.selector} rule={b} value={values[b.selector] ?? b.default ?? ""} onChange={(v) => set(b.selector, v)} problem={problems.get(b.selector)} />
            ))}
          </div>
        </section>

        <section className="card">
          <div className="chd">
            <span className="ct">Connection</span>
            <span className="pill g">Applies without restart</span>
          </div>
          <div className="cp" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
            {shown.map((k) => {
              const required = rules?.required.includes(k) || branchFields.includes(k);
              const id = `prop-${k}`;
              return (
                <div className="fl" key={k} style={{ gridColumn: "1 / -1" }}>
                  <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <label htmlFor={id} className="mono">
                      {k}
                    </label>
                    {!required && <span className="hint">optional</span>}
                    {secret(k) && <span className="pill x pill--xs">Masked</span>}
                    {!required && (
                      <button
                        type="button"
                        className="btn btn--sm"
                        style={{ marginLeft: "auto" }}
                        aria-label={`Remove ${k}`}
                        onClick={() => {
                          setExtra((p) => p.filter((x) => x !== k));
                          setValues((p) => {
                            const n = { ...p };
                            delete n[k];
                            return n;
                          });
                        }}
                      >
                        Remove
                      </button>
                    )}
                  </span>
                  <input
                    id={id}
                    className="inp mono"
                    type={secret(k) ? "password" : "text"}
                    autoComplete="off"
                    required={required}
                    value={values[k] ?? ""}
                    onChange={(e) => set(k, e.target.value)}
                    aria-invalid={problems.has(k)}
                  />
                  {problems.has(k) && <span className="hint" style={{ color: "var(--red)" }}>{problems.get(k)}</span>}
                </div>
              );
            })}
            {shown.length === 0 && <span className="muted">This connector needs no connection settings.</span>}
            {(rules === null || (rules.prefixes.length > 0)) && (
              <div className="fl" style={{ gridColumn: "1 / -1" }}>
                {rules === null ? "Add a property" : `Add a property starting with ${rules.prefixes.join(", ")}`}
                <span style={{ display: "flex", gap: 8 }}>
                  <input className="inp mono" placeholder="property" aria-label="New property name" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
                  <button
                    type="button"
                    className="btn"
                    disabled={newKey.trim() === "" || shown.includes(newKey.trim()) || (rules !== null && !rules.prefixes.some((p) => newKey.trim().startsWith(p)))}
                    onClick={() => {
                      setExtra((p) => [...p, newKey.trim()]);
                      setNewKey("");
                    }}
                  >
                    + Add property
                  </button>
                </span>
              </div>
            )}
            {WIRES_CERTIFICATE.has(connector) && (
              <label className="fl" style={{ gridColumn: "1 / -1" }}>
                Client certificate <span className="hint">Optional — Apchi wires the certificate and key in for you</span>
                <select className="inp" value={certificate} onChange={(e) => setCertificate(e.target.value)}>
                  <option value="">None</option>
                  {(certificates.data ?? []).map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        </section>

        {addable.length > 0 && (
          <section className="card">
            <div className="chd">
              <span className="ct">Optional settings</span>
              <span className="sm muted">add only what you need</span>
            </div>
            <div className="cp" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {addable.map((k) => (
                <button type="button" key={k} className="btn mono" onClick={() => setExtra((p) => [...p, k])}>
                  + {k}
                </button>
              ))}
            </div>
          </section>
        )}

        <ErrorNote error={stage.error} />
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" className="btn" onClick={onCancel}>
            {editing ? "Cancel" : "Back"}
          </button>
          <span className="sp" />
          <button type="submit" className="btn pri" disabled={stage.isPending}>
            {stage.isPending ? "Staging…" : editing ? "Stage changes" : "Stage catalog"}
          </button>
        </div>
      </form>

      <aside style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
        <section className="card cp">
          <div className="lab" style={{ marginBottom: 10 }}>What happens next</div>
          <div className="kv" style={{ gridTemplateColumns: "auto 1fr", alignItems: "start" }}>
            <span className="pill b">1</span>
            <span>
              <b>Staged</b> in the shared Candidate — not on the cluster
            </span>
            <span className="pill x">2</span>
            <span>
              <b>Validate</b> to check it connects (1–5 min). There is no quicker per-catalog test.
            </span>
            <span className="pill x">3</span>
            <span>
              <b>Apply</b> {editing ? "changes it" : "creates it"} on the running cluster, no restart
            </span>
          </div>
        </section>
        <section className="card">
          <div className="chd">
            <span className="lab">API request</span>
            <span className="sp" />
            <span className="mono sm muted">
              {editing ? "PATCH" : "POST"} /api/v1/catalogs
              {editing ? `/${initial.name}` : ""}
            </span>
          </div>
          <pre className="code">{preview}</pre>
        </section>
      </aside>
    </div>
  );
}

function Branch({
  rule,
  value,
  onChange,
  problem,
}: {
  rule: S["BranchRules"];
  value: string;
  onChange: (v: string) => void;
  problem?: string;
}) {
  const known = rule.allowed ?? Object.keys(rule.values);
  if (rule.allowed === null || rule.allowed === undefined) {
    // Any value is accepted: free entry, with the values Apchi knows offered as suggestions.
    const list = `branch-${rule.selector}`;
    return (
      <label className="fl">
        <span className="mono">{rule.selector}</span>
        <input className="inp mono" list={list} value={value} onChange={(e) => onChange(e.target.value)} />
        <datalist id={list}>
          {known.map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
        {problem && <span className="hint" style={{ color: "var(--red)" }}>{problem}</span>}
      </label>
    );
  }
  return (
    <div className="fl" role="radiogroup" aria-label={rule.selector}>
      <span className="mono">{rule.selector}</span>
      <span style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {known.map((v) => (
          <button key={v} type="button" role="radio" aria-checked={value === v} className={`tc${value === v ? " sel" : ""}`} onClick={() => onChange(v)}>
            {v}
          </button>
        ))}
      </span>
      {problem && <span className="hint" style={{ color: "var(--red)" }}>{problem}</span>}
    </div>
  );
}
