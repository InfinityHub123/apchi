import { useHealth, useReview } from "./api/queries";
import { useIdentity, useLogout } from "./auth/useAuth";
import { SignInScreen } from "./auth/SignInScreen";
import { Pill, CostPill } from "./ui/Pill";
import { Overview } from "./screens/Overview";
import { SECTIONS } from "./sections";
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
  const identity = useIdentity();

  // The console is not rendered at all for a caller Apchi does not know. Hiding controls
  // while still fetching would show an Operator a shell full of 403s and call it an
  // interface.
  if (identity.isPending) return <Booting />;
  if (identity.isError) return <SignInScreen />;

  return <Console />;
}

function Booting() {
  return (
    <div className="signin">
      <p className="note">Asking Apchi who you are…</p>
    </div>
  );
}

function Console() {
  const identity = useIdentity();
  const health = useHealth();
  const review = useReview();

  const cost = review.data?.cost;
  const unhealthy = health.data !== undefined && health.data.status !== "ok";

  return (
    <div className="app">
      <TopBar
        caller={identity.data?.subject}
        role={identity.data?.role}
        via={identity.data?.via}
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
          <Overview review={review.data} error={review.error} pending={review.isPending} />
        </main>
      </div>
    </div>
  );
}

function TopBar({
  caller,
  role,
  via,
  status,
}: {
  caller?: string;
  role?: string;
  via?: string;
  status: string;
}) {
  const logout = useLogout();
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
      {/* `none` mode has no session to end, so offering it would be a control that does
          nothing. */}
      {via !== undefined && via !== "none" && (
        <button
          type="button"
          className="topbar__signout"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
        >
          Sign out
        </button>
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
      <section className="strip strip--clean" aria-label="Candidate">
        <span className="strip__title strip__title--clean">No staged changes</span>
        <span className="strip__meta">
          {review?.base_snapshot == null
            ? "no Snapshot yet"
            : `Snapshot ${review.base_snapshot}`}
        </span>
      </section>
    );
  }

  const changes = review.sections.reduce((n, s) => n + (s.changes?.length ?? 0), 0);
  const restarts = cost?.restarts_coordinator ?? false;
  const atRisk = cost?.queries_at_risk ?? null;

  return (
    <section className="strip strip--staged" aria-label="Candidate">
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
    </section>
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


