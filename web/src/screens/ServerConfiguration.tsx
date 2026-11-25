import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  FILES,
  type FileName,
  type Role,
  filled,
  saveServerConfiguration,
  useRenderedFiles,
  useServerConfiguration,
} from "../admin/requests";
import { Value } from "../review/ChangeCard";
import { ErrorNote, Loading, PageHead } from "../ui/States";

/** Keys whose values are credentials more often than not -- the test the catalog form uses. */
const secret = (key: string) => /password|secret|token|credential/i.test(key);

const ROLES: { key: Role; label: string; restart: string }[] = [
  {
    key: "coordinator",
    label: "Coordinator",
    restart: "A change here restarts the coordinator at the next Apply, which ends every running query.",
  },
  {
    key: "worker",
    label: "Workers",
    restart: "A change here restarts only the workers, one at a time, each finishing its tasks first — no query is lost.",
  },
];

/**
 * Trino's own files -- config.properties, node.properties, log.properties -- for the coordinator
 * and the workers. Apchi renders them: the lines that name what the chart creates (ports,
 * Services, paths) and the sizing that has to fit the pod come from the chart, and every other
 * property is the Admin's, here, with no schema over it. Trino judges them on the validation
 * probe at the next Apply.
 *
 * Values are shown, unlike a raw file's: these are named properties an Admin edits one at a
 * time. Credential-looking ones are masked until asked for -- against a screen-share, not as a
 * control, since the API returns them in full (UI-BRIEF §12).
 */
export function ServerConfigurationScreen() {
  const stored = useServerConfiguration();
  const rendered = useRenderedFiles();
  const queries = useQueryClient();
  const [role, setRole] = useState<Role>("coordinator");
  const [draft, setDraft] = useState(() => filled(undefined));
  const original = useMemo(() => filled(stored.data), [stored.data]);
  useEffect(() => setDraft(filled(stored.data)), [stored.data]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(original);

  const save = useMutation({
    mutationFn: () => saveServerConfiguration(draft),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["admin", "server-configuration"] }),
  });

  const edit = (file: FileName, next: Record<string, string>) =>
    setDraft((d) => ({ ...d, [role]: { ...d[role], [file]: next } }));

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <PageHead
        crumb={<Link to="/admin">Admin values</Link>}
        title="Server configuration"
        sub="Trino's own files for the coordinator and the workers. Wiring and sizing come from the chart; everything else is set here."
      />
      <div className="row">
        <div style={{ flex: "999 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          <span role="tablist" style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {ROLES.map((r) => (
              <button
                key={r.key}
                type="button"
                role="tab"
                aria-selected={role === r.key}
                className={`tc${role === r.key ? " sel" : ""}`}
                onClick={() => setRole(r.key)}
              >
                {r.label}
              </button>
            ))}
          </span>
          <div className="note note--flush">{ROLES.find((r) => r.key === role)?.restart}</div>
          <ErrorNote error={stored.error} flush={false} />
          {stored.data === undefined && !stored.error ? (
            <div className="card cp">
              <Loading what="the server configuration" />
            </div>
          ) : (
            FILES.map((file) => (
              <PropertiesCard key={`${role}-${file}`} file={file} properties={draft[role][file]} onChange={(next) => edit(file, next)} />
            ))
          )}
          <section className="card cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <button type="button" className="btn pri" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
                Save both roles
              </button>
              <button type="button" className="btn" disabled={!dirty || save.isPending} onClick={() => setDraft(original)}>
                Discard changes
              </button>
              {dirty && <span className="sm muted">Unsaved changes</span>}
            </div>
            {save.isSuccess && !dirty && (
              <div className="note note--blue note--flush">
                Saved. Nothing changes on the cluster until the next Apply — an <Link to="/admin">Admin Apply</Link> or an Operator's.
              </div>
            )}
            <ErrorNote error={save.error} />
            <div className="sm muted">
              <b>Refused when saved:</b> wiring and sizing — the message names the chart value that owns each — and any key or value
              Trino would read differently from how it is written, such as a line break. Everything else is Trino's to judge, on a
              throwaway coordinator before the cluster sees it; if the cluster fails anyway, Auto Rollback restores what it had.
            </div>
          </section>
        </div>
        <RenderedFiles role={role} files={rendered.data?.[role]} error={rendered.error} />
      </div>
    </div>
  );
}

function PropertiesCard({
  file,
  properties,
  onChange,
}: {
  file: FileName;
  properties: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const entries = Object.entries(properties).sort(([a], [b]) => a.localeCompare(b));
  const add = () => {
    if (!key.trim()) return;
    onChange({ ...properties, [key.trim()]: value });
    setKey("");
    setValue("");
  };
  const placeholder = file === "log.properties" ? "io.trino.plugin.postgresql" : file === "node.properties" ? "node.internal-address-source" : "query.max-execution-time";
  return (
    <section className="card" aria-label={file}>
      <div className="chd">
        <span className="ct mono">{file}</span>
        <span className="sm muted">{entries.length === 0 ? "nothing set" : `${entries.length} set`}</span>
      </div>
      {entries.length > 0 && (
        <div className="tbl-wrap">
          <table className="nw">
            <thead>
              <tr>
                <th>Property</th>
                <th>Value</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {entries.map(([k, v]) => (
                <tr key={k}>
                  <td className="mono">{k}</td>
                  <td>
                    <Value value={v} secret={secret(k)} />
                  </td>
                  <td className="right">
                    <button
                      type="button"
                      className="btn btn--sm dng"
                      aria-label={`Remove ${k}`}
                      onClick={() => onChange(Object.fromEntries(entries.filter(([other]) => other !== k)))}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form
        className="cp"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <label className="fl" style={{ flex: "2 1 220px" }}>
          Property
          <input className="inp mono" aria-label={`${file} property`} placeholder={placeholder} value={key} onChange={(e) => setKey(e.target.value)} />
        </label>
        <label className="fl" style={{ flex: "1 1 160px" }}>
          Value
          <input
            className="inp mono"
            aria-label={`${file} value`}
            type={secret(key) ? "password" : "text"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        <button type="submit" className="btn" disabled={!key.trim()}>
          {key.trim() in properties ? "Replace" : "Add"}
        </button>
      </form>
    </section>
  );
}

/** What the next Apply writes for one role, wiring included -- the one place it is whole. */
function RenderedFiles({ role, files, error }: { role: Role; files: Record<string, string> | undefined; error: unknown }) {
  const [shown, setShown] = useState(false);
  const mask = (content: string) =>
    shown
      ? content
      : content
          .split("\n")
          .map((line) => {
            const at = line.indexOf("=");
            return at > 0 && secret(line.slice(0, at)) ? `${line.slice(0, at)}=••••••••` : line;
          })
          .join("\n");
  const anySecret = Object.values(files ?? {}).some((content) =>
    content.split("\n").some((line) => line.indexOf("=") > 0 && secret(line.slice(0, line.indexOf("=")))),
  );
  return (
    <section className="card" style={{ flex: "1 1 380px", minWidth: 0, alignSelf: "flex-start" }} aria-label="Rendered files">
      <div className="chd">
        <span className="ct">As the next Apply writes them</span>
        {anySecret && (
          <button type="button" className="btn btn--sm" onClick={() => setShown((s) => !s)}>
            {shown ? "Mask values" : "Show values"}
          </button>
        )}
      </div>
      <ErrorNote error={error} flush={false} />
      {files === undefined && !error ? (
        <div className="cp">
          <Loading what="the rendered files" />
        </div>
      ) : (
        <div className="cp" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="sm muted">
            Saved values only. The lines above the Admin's are the chart's wiring and sizing, which no one edits here.
          </div>
          {Object.entries(files ?? {}).map(([path, content]) => (
            <div key={`${role}-${path}`}>
              <div className="sm mono muted">{path}</div>
              <pre className="mono" style={{ fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-all", margin: "4px 0 0" }}>
                {mask(content) || <span className="muted">(empty)</span>}
              </pre>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
