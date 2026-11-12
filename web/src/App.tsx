import type { ReactNode } from "react";
import { useHealth, useReview, useCaller } from "./api/queries";
import { Pill, CostPill } from "./ui/Pill";
import { SECTIONS, sectionLabel, sectionRestarts } from "./sections";
import type { Review } from "./api/client";

/**
 * The shell, and the five things §21 says must be visible at all times: cluster health, the
 * current Snapshot, whether the Candidate has staged changes and by whom, which of them force
 * a restart, and the available actions.
 *
 * The Candidate strip sits above the work area on every screen rather than on a review page
 * of its own, because a staged change nobody can see from where they are working is the
 * failure this product exists to prevent.
 */
export default function App() {
  const health = useHealth();
  const review = useReview();
  const caller = useCaller();

  const cost = review.data?.cost;
  const unhealthy = health.data !== undefined && health.data.status !== "ok";

  return (
    <div className="app">
      <TopBar
        caller={caller.data?.subject}
        role={caller.data?.role}
        status={health.isPending ? "checking" : (health.data?.status ?? "unreachable")}
      />

      {unhealthy && <UnhealthyBand mongo={health.data?.mongo} />}

      <CandidateStrip review={review.data} pending={review.isPending} cost={cost} />

      <div className="body">
        <SectionRail
          counts={Object.fromEntries(
            (review.data?.sections ?? []).map((s) => [s.section, s.changes?.length ?? 0]),
          )}
        />
        <main className="work">
          <ReviewScreen review={review.data} error={review.error} pending={review.isPending} />
        </main>
      </div>
    </div>
  );
}

function TopBar({
  caller,
  role,
  status,
}: {
  caller?: string;
  role?: string;
  status: string;
}) {
  const tone = status === "ok" ? "green" : status === "checking" ? "gray" : "red";
  const label =
    status === "ok" ? "Healthy" : status === "checking" ? "Checking" : "Unreachable";
  return (
    <header className="topbar">
      <span className="topbar__mark" aria-hidden="true">
        A
      </span>
      <span className="topbar__name">Apchi</span>
      <span className="rule-v" aria-hidden="true" />
      <Pill tone={tone} dot>
        {label}
      </Pill>
      <span className="spacer" />
      {caller !== undefined && (
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {caller}
          {role !== undefined && <span style={{ color: "var(--ink-4)" }}> · {role}</span>}
        </span>
      )}
    </header>
  );
}

/**
 * §21 fixes this wording: "Configuration was applied, but the Trino cluster did not pass
 * health verification. The previous Snapshot remains the known-good configuration." It is
 * product copy, not placeholder text, so the sentence is reproduced and the diagnosis goes
 * underneath it.
 */
function UnhealthyBand({ mongo }: { mongo?: string }) {
  return (
    <div className="band" role="alert">
      <div style={{ flex: 1 }}>
        <div className="band__title">The Trino cluster did not pass health verification</div>
        <div className="band__detail">
          The previous Snapshot remains the known-good configuration.
          {mongo !== undefined && ` Apchi's own database reports: ${mongo}.`}
        </div>
      </div>
    </div>
  );
}

function CandidateStrip({
  review,
  cost,
  pending,
}: {
  review: Review | undefined;
  cost: Review["cost"] | undefined;
  pending: boolean;
}) {
  if (pending) {
    return (
      <div className="strip strip--clean">
        <span className="strip__meta">Reading the Candidate…</span>
      </div>
    );
  }

  if (review === undefined || !review.has_changes) {
    return (
      <div className="strip strip--clean">
        <span className="strip__title strip__title--clean">No staged changes</span>
        <span className="strip__meta">
          {review?.base_snapshot == null
            ? "no Snapshot yet"
            : `Snapshot ${review.base_snapshot}`}
        </span>
      </div>
    );
  }

  const changes = review.sections.reduce((n, s) => n + (s.changes?.length ?? 0), 0);
  const restarts = cost?.restarts_coordinator ?? false;
  const atRisk = cost?.queries_at_risk ?? null;

  return (
    <div className="strip strip--staged">
      <span className="strip__title">Candidate</span>
      <span className="strip__meta">
        {review.base_snapshot == null
          ? "no base Snapshot"
          : `based on Snapshot ${review.base_snapshot}`}{" "}
        · {changes} {changes === 1 ? "change" : "changes"}
      </span>
      <span className="strip__rule" aria-hidden="true" />
      <CostPill restarts={restarts} />
      {restarts && atRisk !== null && <Pill tone="red">{atRisk} queries at risk</Pill>}
      <span className="spacer" />
      <div className="strip__actions">
        <button type="button" className="btn">
          Reset
        </button>
        <button type="button" className="btn">
          Validate
        </button>
        <button type="button" className="btn btn--primary">
          Apply
        </button>
      </div>
    </div>
  );
}

function SectionRail({ counts }: { counts: Record<string, number> }) {
  return (
    <nav className="rail" aria-label="Sections">
      <div className="rail__label">Sections</div>
      {SECTIONS.map((s, i) => {
        const n = counts[s.key] ?? 0;
        return (
          <a
            key={s.key}
            href={`#${s.key}`}
            className={`rail__item${i === 0 ? " rail__item--on" : ""}`}
            aria-current={i === 0 ? "page" : undefined}
          >
            <span className="rail__name">{s.label}</span>
            {n > 0 ? (
              <Pill tone={s.restarts ? "amber" : "green"}>{n}</Pill>
            ) : (
              <span className="rail__none" aria-label="no changes">
                —
              </span>
            )}
          </a>
        );
      })}
    </nav>
  );
}

function ReviewScreen({
  review,
  error,
  pending,
}: {
  review: Review | undefined;
  error: unknown;
  pending: boolean;
}) {
  if (pending) return <p className="note">Reading the Candidate…</p>;
  if (error !== null && error !== undefined)
    return <p className="note note--red">{(error as Error).message}</p>;
  if (review === undefined) return <p className="note">Nothing to show.</p>;

  const changed = review.sections.filter((s) => (s.changes?.length ?? 0) > 0);

  return (
    <>
      <h1 className="title">Review</h1>
      <p className="lede">
        {review.has_changes
          ? "Nothing here has reached the cluster. An Apply is the only thing that changes it."
          : "The Candidate matches the committed Snapshot."}
      </p>

      {changed.length === 0 ? (
        <p className="note">
          No Section has staged changes. Edit a Section to stage one and it will appear here
          before it reaches Trino.
        </p>
      ) : (
        <div className="panel">
          <table className="table">
            <caption className="sr-only">
              Every staged change in the Candidate, by Section, with what it costs the cluster
            </caption>
            <thead>
              <tr>
                <th scope="col">Section</th>
                <th scope="col">Change</th>
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
    </>
  );
}

/**
 * Added, changed or removed, as a glyph and a word.
 *
 * Colour alone would not carry it: things that must be told apart differ in lightness too,
 * and an Operator reading a long diff should not have to compare two greens. The word is
 * there for a screen reader, which cannot see either.
 */
function ChangeMark({ change }: { change: "added" | "changed" | "removed" }) {
  const mark = { added: "+", changed: "~", removed: "−" }[change];
  return (
    <span className={`mark mark--${change}`}>
      <span className="sr-only">{change} </span>
      {mark}
    </span>
  );
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="note">{children}</p>;
}
