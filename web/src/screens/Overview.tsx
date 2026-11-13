import { Pill, CostPill } from "../ui/Pill";
import { sectionLabel, sectionRestarts } from "../sections";
import type { Review } from "../api/client";
import {
  attribute,
  useExpiringCertificates,
  useRecentActivity,
  useSnapshots,
} from "../api/overview";

/**
 * What an Operator sees first.
 *
 * §21 requires five things to be visible at all times, and the shell carries three of them.
 * This screen carries the other two: the current Snapshot in full, and the whole Candidate
 * diff -- every Section, not only the current Operator's changes -- with who staged each one.
 */
export function Overview({
  review,
  error,
  pending,
}: {
  review: Review | undefined;
  error: unknown;
  pending: boolean;
}) {
  const snapshots = useSnapshots();
  const activity = useRecentActivity();
  const expiring = useExpiringCertificates();

  if (pending) return <p className="note">Reading the Candidate…</p>;
  if (error !== null && error !== undefined)
    return <p className="note note--red">{(error as Error).message}</p>;
  if (review === undefined) return <p className="note">Nothing to show.</p>;

  const committed = snapshots.data?.items?.[0];
  const by = attribute(activity.data?.items);
  const changed = review.sections.filter((s) => (s.changes?.length ?? 0) > 0);
  const operators = new Set(
    changed.flatMap((s) =>
      (s.changes ?? []).map((c) => by.get(`${s.section}:${c.resource}`)).filter(Boolean),
    ),
  );

  return (
    <>
      <h1 className="title">Overview</h1>
      <p className="lede">
        {review.has_changes
          ? "Nothing below has reached the cluster. An Apply is the only thing that changes it."
          : "The Candidate matches the committed Snapshot. There is nothing staged."}
      </p>

      <div className="facts">
        <Fact label="Committed configuration">
          {committed === undefined ? (
            <Unknown>No Snapshot yet</Unknown>
          ) : (
            <>
              <span className="fact__value">Snapshot {committed.number}</span>
              <span className="fact__meta">{when(committed.created_at)}</span>
            </>
          )}
        </Fact>

        <Fact label="Candidate">
          {review.has_changes ? (
            <>
              <span className="fact__value">
                {count(changed.flatMap((s) => s.changes ?? []).length, "change")}
              </span>
              <span className="fact__meta">
                {operators.size === 0
                  ? "staged; the audit does not say by whom"
                  : `staged by ${[...operators].join(", ")}`}
              </span>
            </>
          ) : (
            <Unknown>Nothing staged</Unknown>
          )}
        </Fact>

        <Fact label="Expiring certificates">
          {expiring.isPending ? (
            <Unknown>Reading…</Unknown>
          ) : (expiring.data?.length ?? 0) === 0 ? (
            <Unknown>None inside the window</Unknown>
          ) : (
            <>
              <span className="fact__value">{expiring.data?.length}</span>
              <span className="fact__meta">
                soonest {expiring.data?.[0]?.name} on {day(expiring.data?.[0]?.not_after)}
              </span>
            </>
          )}
        </Fact>
      </div>

      <h2 className="subtitle">Staged changes</h2>
      {changed.length === 0 ? (
        <p className="note">
          No Section has staged changes. Edit a Section to stage one and it will appear here
          before it reaches Trino.
        </p>
      ) : (
        <div className="panel">
          <table className="table">
            <caption className="sr-only">
              Every staged change in the Candidate, by Section, with who staged it and what it
              costs the cluster
            </caption>
            <thead>
              <tr>
                <th scope="col">Section</th>
                <th scope="col">Change</th>
                <th scope="col">By</th>
                <th scope="col" className="right">
                  Costs the cluster
                </th>
              </tr>
            </thead>
            <tbody>
              {changed.flatMap((s) =>
                (s.changes ?? []).map((c, i) => (
                  <tr key={`${s.section}:${c.resource}`}>
                    <td>
                      {i === 0 ? (
                        <span className="strong">{sectionLabel(s.section)}</span>
                      ) : (
                        <span className="muted" aria-hidden="true">
                          ·
                        </span>
                      )}
                    </td>
                    <td className="mono">
                      <ChangeMark change={c.change} /> {c.resource}
                    </td>
                    <td className="muted">
                      {by.get(`${s.section}:${c.resource}`) ?? (
                        <span title="The audit trail does not attribute this change.">—</span>
                      )}
                    </td>
                    <td className="right">
                      <CostPill restarts={sectionRestarts(s.section)} />
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="subtitle">Recent Snapshots</h2>
      {snapshots.isPending ? (
        <p className="note">Reading the Snapshot history…</p>
      ) : (snapshots.data?.items?.length ?? 0) === 0 ? (
        <p className="note">
          No Snapshot has been committed yet. The first Apply makes one.
        </p>
      ) : (
        <div className="panel">
          <table className="table">
            <caption className="sr-only">The most recent committed Snapshots</caption>
            <thead>
              <tr>
                <th scope="col">Snapshot</th>
                <th scope="col">Committed</th>
                <th scope="col">Apply</th>
              </tr>
            </thead>
            <tbody>
              {(snapshots.data?.items ?? []).map((s, i) => (
                <tr key={s.number}>
                  <td className="strong">
                    {s.number}
                    {i === 0 && (
                      <>
                        {" "}
                        <Pill tone="green">Current</Pill>
                      </>
                    )}
                  </td>
                  <td>{when(s.created_at)}</td>
                  <td className="mono">{s.apply_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="fact">
      <span className="label">{label}</span>
      {children}
    </div>
  );
}

function Unknown({ children }: { children: React.ReactNode }) {
  return <span className="fact__none">{children}</span>;
}

function ChangeMark({ change }: { change: "added" | "changed" | "removed" }) {
  const mark = { added: "+", changed: "~", removed: "−" }[change];
  return (
    <span className={`mark mark--${change}`}>
      <span className="sr-only">{change} </span>
      {mark}
    </span>
  );
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** Absolute, never "2 hours ago": an Operator correlating with a Trino log needs the clock. */
function when(iso: string | undefined): string {
  if (iso === undefined) return "—";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function day(iso: string | undefined): string {
  if (iso === undefined) return "—";
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}
