import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, unwrap, type S } from "../api/client";
import { LoadOlder, usePaged } from "../api/paged";
import { useStartValidation } from "../api/queries";
import { validationFailed } from "../applies/outcome";
import { useChangedSince } from "../review/changedSince";
import { sectionLabel } from "../sections";
import { dayTime, duration, plural } from "../ui/format";
import { Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

function outcomePill(v: S["ValidationRecord"]) {
  if (v.outcome === "running") return <Pill tone="b" dot>Running · {duration(v.started_at)}</Pill>;
  if (v.outcome === "failed") return <Pill tone="r">Failed · {(v.failures ?? []).length}</Pill>;
  return (
    <Pill tone="g">
      Passed{(v.warnings ?? []).length > 0 ? ` · ${plural((v.warnings ?? []).length, "warning")}` : ""}
    </Pill>
  );
}

/**
 * A throwaway coordinator trying the Candidate as it is now. Changes nothing and freezes
 * nothing, so it can be run as often as anyone likes -- and it is the only per-catalog
 * connection test there is.
 */
export function ValidationsScreen() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const start = useStartValidation();
  const paged = usePaged(
    ["validations", "list"],
    (before) => api.GET("/api/v1/validations", { params: { query: { limit: 25, before } } }).then(unwrap),
    { refetchInterval: 5_000 },
  );
  const selected = params.get("id") ?? paged.items[0]?.id ?? null;

  return (
    <div className="page">
      <PageHead
        title="Validations"
        sub="A throwaway Trino coordinator tries the Candidate as it is now · 1–5 minutes · changes nothing and does not freeze the Candidate"
      >
        <button
          type="button"
          className="btn pri"
          disabled={start.isPending}
          onClick={() => start.mutate(undefined, { onSuccess: (v) => navigate(`/validations?id=${v.id}`) })}
        >
          {start.isPending ? "Starting…" : "Validate the Candidate"}
        </button>
      </PageHead>
      <ErrorNote error={start.error ?? paged.error} />
      {!paged.loaded && !paged.error && <Loading what="the Validations" />}
      {paged.loaded && paged.items.length === 0 && (
        <div className="card cp muted">Nothing has been validated yet.</div>
      )}
      {paged.items.length > 0 && (
        <div className="row">
          <section className="card" style={{ flex: "1 1 340px", minWidth: 0 }}>
            <div className="tbl-wrap">
              <table className="nw">
                <thead>
                  <tr>
                    <th>Validation</th>
                    <th>Started</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {paged.items.map((v) => (
                    <tr
                      key={v.id}
                      className={`click${v.id === selected ? " sel" : ""}`}
                      onClick={() => setParams({ id: v.id })}
                      aria-selected={v.id === selected}
                    >
                      <td>
                        <span className="nm">{v.id}</span>
                      </td>
                      <td>{dayTime(v.started_at)}</td>
                      <td>{outcomePill(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <LoadOlder paged={paged} />
          </section>
          <div style={{ flex: "999 1 560px", minWidth: 0 }}>{selected !== null && <ValidationDetail id={selected} />}</div>
        </div>
      )}
    </div>
  );
}

function ValidationDetail({ id }: { id: string }) {
  const read = useQuery({
    queryKey: ["validations", id],
    queryFn: () => api.GET("/api/v1/validations/{validation_id}", { params: { path: { validation_id: id } } }).then(unwrap),
    refetchInterval: (q) => (q.state.data?.outcome === "running" ? 3_000 : false),
  });
  const v = read.data;
  const since = useChangedSince(v?.started_at);

  if (read.error) return <ErrorNote error={read.error} />;
  if (v === undefined) return <Loading what={`Validation ${id}`} />;

  return (
    <section className="card">
      <div className="chd">
        {outcomePill(v)}
        <span className="ct mono">{v.id}</span>
        <span className="sm muted">
          {dayTime(v.started_at)} · on {v.base_snapshot == null ? "an empty base" : `Snapshot ${v.base_snapshot}`} + the
          Candidate
          {v.finished_at != null && ` · ${duration(v.started_at, v.finished_at)}`}
        </span>
      </div>
      {v.outcome === "running" && (
        <div className="cp muted">Running. A throwaway coordinator is loading the Candidate; this takes 1–5 minutes.</div>
      )}
      {v.outcome === "failed" && (
        <>
          <div className="cp" style={{ fontWeight: 550 }}>{validationFailed((v.failures ?? []))}</div>
          <Failures rows={(v.failures ?? [])} />
          <div className="note" style={{ marginTop: 15 }}>
            Every failure is listed, not just the first. A Validation never changes the cluster and can be run
            again as often as needed.
          </div>
        </>
      )}
      {(v.warnings ?? []).map((w, i) => (
        <div className="cr" key={i}>
          <Pill tone="a">Warning</Pill>
          <span>
            {w.resource != null && <span className="mono">{w.resource}: </span>}
            {w.reason}
          </span>
        </div>
      ))}
      {v.outcome === "passed" && (v.warnings ?? []).length === 0 && (
        <div className="cp">Trino loaded the whole Candidate. Nothing to report.</div>
      )}
      {v.outcome !== "running" && since !== null && since > 0 && (
        <div className="note note--amber">
          Since this Validation, the Candidate changed {since === 1 ? "once" : `${since} times`}. Validate again to
          check the current Candidate.
        </div>
      )}
    </section>
  );
}

function Failures({ rows }: { rows: S["ValidationFailure"][] }) {
  return (
    <div className="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Section</th>
            <th>Resource</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f, i) => (
            <tr key={i}>
              <td>{f.section == null ? "—" : sectionLabel(f.section)}</td>
              <td className="mono">{f.resource ?? "—"}</td>
              <td style={{ padding: "10px 14px" }}>{f.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
