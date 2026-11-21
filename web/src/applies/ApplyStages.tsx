import type { S } from "../api/client";
import { sectionLabel } from "../sections";
import { duration, plural } from "../ui/format";
import { Pill, type Tone } from "../ui/Pill";
import { checkLabel, outcomeOf } from "./outcome";

type StepState = "done" | "now" | "failed" | "waiting" | "skipped";

const PILL: Record<StepState, [Tone, string]> = {
  done: ["g", "Done"],
  now: ["b", "In progress"],
  failed: ["r", "Failed"],
  waiting: ["x", "Waiting"],
  skipped: ["x", "Not needed"],
};

/**
 * An Apply's five steps, and where it is in them.
 *
 * Derived from the record's own history, so it reads the same live and a week later. A step
 * the Apply never entered is `waiting` while it runs and `skipped` once it is over, except
 * the Rollout: an Apply that changed nothing Trino adopts by restarting has no Rollout
 * stage at all, which is "not needed", not "never reached".
 */
export function ApplyStages({ record }: { record: S["ApplyRecord"] }) {
  const o = outcomeOf(record);
  const history = record.history ?? [];
  const at = (stage: string) => history.find((e) => e.stage === stage)?.at;
  const order = ["validating", "applying", "rolling_out", "verifying", "committing"];
  const entered = new Set<string>(history.map((e) => e.stage));
  const lastReached = [...order].reverse().find((s) => entered.has(s));
  const failedAt = record.stage === "failed" || record.stage === "incident" ? lastReached : undefined;

  function state(stage: string): StepState {
    if (!entered.has(stage)) {
      if (stage === "rolling_out" && entered.has("verifying")) return "skipped";
      return o.inFlight ? "waiting" : "skipped";
    }
    if (stage === failedAt) return "failed";
    if (o.inFlight && stage === lastReached) return "now";
    return "done";
  }

  const next = (stage: string) => {
    const i = order.indexOf(stage);
    for (const later of [...order.slice(i + 1), "rolling_back", "succeeded", "failed", "incident"]) {
      const t = at(later);
      if (t !== undefined) return t;
    }
    return record.finished_at ?? null;
  };
  const took = (stage: string) => {
    const start = at(stage);
    return start === undefined ? null : duration(start, next(stage));
  };

  const warnings = record.warnings ?? [];
  const failures = record.failures ?? [];
  const checks = record.verification ?? [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <Step n={1} title="Validation" sub="throwaway coordinator" state={state("validating")} took={took("validating")}>
        {failures.length > 0 && (
          <ul style={{ margin: 0, padding: "0 15px 12px 34px" }}>
            {failures.map((f, i) => (
              <li key={i}>
                {f.section != null && <b>{sectionLabel(f.section)}</b>}
                {f.resource != null && <> · <span className="mono">{f.resource}</span></>} — {f.reason}
              </li>
            ))}
          </ul>
        )}
        {warnings.length > 0 && (
          <div className="cr" style={{ gridTemplateColumns: "minmax(0,1fr)" }}>
            <span>
              {plural(warnings.length, "warning")} —{" "}
              {warnings.map((w) => `${w.resource != null ? `${w.resource}: ` : ""}${w.reason}`).join("; ")}
            </span>
          </div>
        )}
      </Step>
      <Step n={2} title="Applying" sub="write configuration" state={state("applying")} took={took("applying")} />
      <Step
        n={3}
        title="Rolling out"
        sub={record.rolled_out || o.inFlight ? "coordinator restart · usually 2–10 minutes" : "no Section needed a restart"}
        state={state("rolling_out")}
        took={took("rolling_out")}
      />
      <Step
        n={4}
        title="Verification"
        sub="coordinator, workers, a smoke query, then each Section adopted — stops at the first failure"
        state={state("verifying")}
        took={took("verifying")}
      >
        {checks.length > 0 && <Checks checks={checks} />}
      </Step>
      <Step
        n={5}
        title="Commit"
        sub={
          record.kind === "admin_apply"
            ? "an Admin Apply creates no Snapshot"
            : record.snapshot != null
              ? `created Snapshot ${record.snapshot}`
              : "creates the next Snapshot"
        }
        state={state("committing")}
        took={took("committing")}
      />
      {entered.has("rolling_back") && (
        <section className="card" style={{ borderColor: "var(--red-bd)" }}>
          <div className="chd" style={{ background: "var(--red-bg)", borderColor: "var(--red-bd)" }}>
            <span className="ct">Auto Rollback</span>
            <span className="sm muted">one attempt, never retried</span>
            <span className="sp" />
            {record.rollback == null ? (
              <Pill tone="b" dot>In progress</Pill>
            ) : record.rollback === "succeeded" ? (
              <Pill tone="g">Put back</Pill>
            ) : (
              <Pill tone="r">Auto Rollback failed</Pill>
            )}
          </div>
        </section>
      )}
      {!o.inFlight && o.message !== null && <OutcomeCard record={record} />}
    </div>
  );
}

function Step({
  n,
  title,
  sub,
  state,
  took,
  children,
}: {
  n: number;
  title: string;
  sub: string;
  state: StepState;
  took: string | null;
  children?: React.ReactNode;
}) {
  const [tone, label] = PILL[state];
  const quiet = state === "waiting" || state === "skipped";
  return (
    <section className="card" style={quiet ? { opacity: 0.75 } : undefined} aria-label={`${title}: ${label}`}>
      <div className="chd" style={children ? undefined : { borderBottom: 0 }}>
        <span className={`dot-n ${state === "done" ? "g" : state === "now" ? "b" : state === "failed" ? "r" : ""}`}>{n}</span>
        <span className="ct">{title}</span>
        <span className="sm muted">{sub}</span>
        <span className="sp" />
        {took !== null && <span className="mono sm muted">{took}</span>}
        <Pill tone={tone} dot={state === "now"}>
          {label}
        </Pill>
      </div>
      {children}
    </section>
  );
}

export function Checks({ checks }: { checks: S["CheckResult"][] }) {
  return (
    <>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th>Check</th>
              <th>Outcome</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.check}>
                <td>{checkLabel(c.check)}</td>
                <td>
                  {c.outcome === "passed" ? (
                    <Pill tone="g">Passed</Pill>
                  ) : c.outcome === "failed" ? (
                    <Pill tone="r">Failed</Pill>
                  ) : (
                    <Pill tone="x">Not reached</Pill>
                  )}
                </td>
                <td className={c.detail ? undefined : "muted"}>{c.detail ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {checks.some((c) => c.outcome === "not_reached") && (
        <div className="note" style={{ marginTop: 12 }}>
          "Not reached" is not a pass: nothing after the failed check was asked.
        </div>
      )}
    </>
  );
}

/** The fixed sentence for how it ended, and the diagnostic underneath it -- both, per §21. */
export function OutcomeCard({ record }: { record: S["ApplyRecord"] }) {
  const o = outcomeOf(record);
  return (
    <section className="card" id="diag" style={{ borderColor: o.tone === "r" ? "var(--red-bd)" : undefined }}>
      <div className="chd">
        <Pill tone={o.tone}>{o.label}</Pill>
        <span className="ct">{o.message}</span>
      </div>
      {record.failure_reason != null && (
        <>
          <div className="chd" style={{ borderBottom: 0, paddingBottom: 6 }}>
            <span className="lab">Diagnostic failure reason</span>
            <span className="sp" />
            <button type="button" className="btn btn--sm" onClick={() => void navigator.clipboard?.writeText(record.failure_reason ?? "")}>
              Copy
            </button>
          </div>
          <pre className="code">{record.failure_reason}</pre>
        </>
      )}
      <div className="cft">
        <span className="sm muted">
          {record.stage === "incident"
            ? "Apchi has stopped touching the cluster and will not retry. Maintenance Mode is on."
            : record.rollback === "succeeded"
              ? `The cluster was put back${record.base_snapshot != null ? ` on Snapshot ${record.base_snapshot}` : ""} and work continued. No Snapshot was created.`
              : "Nothing reached the cluster. No Snapshot was created."}{" "}
          The id <span className="mono">{record.id}</span> is also the id in Apchi's logs.
        </span>
      </div>
    </section>
  );
}
