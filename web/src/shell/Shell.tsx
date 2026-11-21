import { useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import type { Review, S } from "../api/client";
import {
  useApply,
  useCluster,
  useProbe,
  useReset,
  useReview,
  useStartValidation,
} from "../api/queries";
import { useIdentity, useLogout } from "../auth/useAuth";
import { SECTIONS } from "../sections";
import { Dialog } from "../ui/Dialog";
import { clock, dayTime, plural } from "../ui/format";
import { Icon, Logo, type IconName } from "../ui/Icon";
import { Pill } from "../ui/Pill";
import { ErrorNote } from "../ui/States";
import { useAuthorship, othersCount } from "../review/authorship";
import { candidateCounts } from "../review/counts";

/**
 * The frame around every screen, and §21's five things that must be visible from anywhere:
 * the live Snapshot, cluster health, whether the Candidate holds staged changes and whose,
 * which of them force a restart, and what can be done about it.
 *
 * The Candidate bar sits above the work area on every screen rather than on a page of its
 * own, because a staged change nobody can see from where they are working is the failure
 * this product exists to prevent.
 */
export function Shell() {
  const identity = useIdentity();
  const cluster = useCluster();
  const review = useReview();

  const state = cluster.data;
  const incident = state?.last_apply?.stage === "incident";
  const inFlight =
    state?.last_apply != null && state.last_apply.finished_at == null && !incident;

  return (
    <div className="app">
      <TopBar identity={identity.data} state={state} incident={incident} />
      {incident && state?.last_apply != null && <IncidentAlarm applyId={state.last_apply.id} snapshot={state.snapshot ?? null} />}
      <CandidateBar
        review={review.data}
        error={review.error}
        locked={state?.maintenance.engaged ?? false}
        inFlight={inFlight ? (state?.last_apply?.id ?? null) : null}
      />
      <div className="body">
        <Sidebar
          review={review.data}
          admin={identity.data?.role === "admin"}
          enforced={state?.permissions_enforced}
        />
        <main className="main" id="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

const VIA: Record<string, string> = {
  password: "password",
  sso: "SSO",
  token: "token",
  none: "no sign-in",
};

function TopBar({
  identity,
  state,
  incident,
}: {
  identity: S["Identity"] | undefined;
  state: S["ClusterState"] | undefined;
  incident: boolean;
}) {
  const logout = useLogout();

  return (
    <header className="top">
      <Link to="/" style={{ display: "flex", alignItems: "center", gap: 9, textDecoration: "none" }}>
        <Logo />
        <span className="wordmark">apchi</span>
      </Link>
      <span className="vr" aria-hidden="true" />

      {state === undefined ? (
        <span className="sm muted">Reading the cluster…</span>
      ) : incident ? (
        <>
          <Pill tone="r" dot>
            Incident · configuration unknown
          </Pill>
          {state.snapshot != null && (
            <span className="sm muted">Last known-good: Snapshot {state.snapshot}</span>
          )}
        </>
      ) : state.snapshot == null ? (
        <Pill tone="x">No Snapshot yet</Pill>
      ) : (
        <>
          <Pill tone="g">Snapshot {state.snapshot}</Pill>
          {/* A Snapshot exists only once its Apply's Verification passed, so this is true of
              every Snapshot -- and it is history, not a reading of now. */}
          {state.snapshot_at != null && (
            <span className="sm muted">verified at its Apply, {dayTime(state.snapshot_at)}</span>
          )}
        </>
      )}

      <span className="vr" aria-hidden="true" />
      <Coordinator />
      <span className="sp" />

      {state !== undefined && (
        <Link to="/maintenance" style={{ textDecoration: "none" }}>
          {state.maintenance.engaged ? (
            <Pill tone="r">Maintenance Mode on</Pill>
          ) : (
            <Pill tone="x">Maintenance Mode off</Pill>
          )}
        </Link>
      )}
      <span className="vr" aria-hidden="true" />

      {identity !== undefined && (
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="av" aria-hidden="true">
            {identity.subject.slice(0, 1).toUpperCase()}
          </span>
          <span style={{ fontSize: 12.5, fontWeight: 550 }}>{identity.subject}</span>
          <span className="sm muted">
            {identity.role === "admin" ? "Admin" : "Operator"} · {VIA[identity.via] ?? identity.via}
          </span>
        </span>
      )}
      {/* `none` mode has no session to end, so a Sign out there would be a control that
          does nothing. */}
      {identity !== undefined && identity.via !== "none" && identity.via !== "token" && (
        <button
          type="button"
          className="ib"
          aria-label="Sign out"
          title="Sign out"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
        >
          <Icon name="signOut" />
        </button>
      )}
    </header>
  );
}

/**
 * The coordinator as last read. Read once when the console opens and again on "Check
 * again", never on a timer: each reading is a request to the coordinator, and it is stale
 * the moment it arrives, which is why the time it was taken is always beside it.
 */
function Coordinator() {
  const probe = useProbe();

  const reading = probe.data;
  const pill =
    reading === undefined ? (
      probe.isError ? (
        <Pill tone="r" dot>
          Apchi could not ask
        </Pill>
      ) : (
        <Pill tone="x" dot>
          Asking the coordinator…
        </Pill>
      )
    ) : reading.coordinator === "up" ? (
      <Pill tone="g" dot>
        Coordinator up
        {reading.workers != null && ` · ${plural(reading.workers, "worker")}`}
      </Pill>
    ) : reading.coordinator === "starting" ? (
      <Pill tone="a" dot>
        Coordinator starting
      </Pill>
    ) : (
      <Pill tone="r" dot>
        Coordinator unreachable
      </Pill>
    );

  return (
    <>
      {pill}
      {reading !== undefined && <span className="sm muted">read {clock(reading.at)}</span>}
      <button
        type="button"
        className="btn btn--sm"
        onClick={() => void probe.refetch()}
        disabled={probe.isFetching}
      >
        {probe.isFetching ? "Checking…" : "Check again"}
      </button>
    </>
  );
}

/**
 * §21 fixes the first sentence -- product copy, not placeholder text. What follows it is the
 * Apply's own `operator_message` when Apchi recorded one.
 */
function IncidentAlarm({ applyId, snapshot }: { applyId: string; snapshot: number | null }) {
  const apply = useApply(applyId);
  return (
    <div className="alarm" role="alert">
      <Icon name="alert" size={18} />
      <div style={{ flex: "1 1 420px" }}>
        <div className="alarm__title">
          The Trino cluster is currently unhealthy after applying configuration. Please contact
          our team.
        </div>
        <div className="alarm__detail">
          {apply.data?.operator_message ??
            `Apply ${applyId} failed and its Auto Rollback failed too. Maintenance Mode is on; editing is disabled.`}
          {snapshot != null && (
            <>
              {" "}
              Last known-good: <span className="mono">Snapshot {snapshot}</span>.
            </>
          )}
        </div>
      </div>
      <Link className="btn" to={`/applies/${applyId}`}>
        View diagnostic
      </Link>
    </div>
  );
}

function CandidateBar({
  review,
  error,
  locked,
  inFlight,
}: {
  review: Review | undefined;
  error: unknown;
  locked: boolean;
  inFlight: string | null;
}) {
  const navigate = useNavigate();
  const reset = useReset();
  const validate = useStartValidation();
  const authorship = useAuthorship(review);
  const [confirmReset, setConfirmReset] = useState(false);

  if (error != null) {
    return (
      <div className="bar bar--quiet">
        <ErrorNote error={error} />
      </div>
    );
  }
  if (review === undefined) {
    return (
      <div className="bar bar--quiet">
        <span className="sm">Reading the Candidate…</span>
      </div>
    );
  }

  const counts = candidateCounts(review);
  const base =
    review.base_snapshot == null ? "no Snapshot yet" : `Snapshot ${review.base_snapshot}`;

  if (locked || inFlight !== null) {
    return (
      <div className="bar bar--locked">
        <Pill tone="x">{locked ? "Candidate locked" : "Candidate frozen"}</Pill>
        <span className="sm">
          {locked
            ? `${plural(counts.total, "staged change")} ${counts.total === 1 ? "is" : "are"} read-only until Maintenance Mode is released.`
            : `Apply ${inFlight} is in flight. Changes are refused until it finishes, so that what is committed is what was verified.`}
        </span>
        <span style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
          {inFlight !== null && (
            <Link className="btn" to={`/apply?id=${inFlight}`}>
              Follow the Apply
            </Link>
          )}
          <button type="button" className="btn" disabled>
            Reset
          </button>
          <button type="button" className="btn" disabled>
            Validate
          </button>
          <button type="button" className="btn" disabled>
            Apply
          </button>
        </span>
      </div>
    );
  }

  if (!review.has_changes) {
    return (
      <div className="bar bar--quiet">
        <Pill tone="x">Nothing staged</Pill>
        <span className="sm">
          {review.base_snapshot == null
            ? "Nothing has been applied yet, and nothing is staged."
            : `The Candidate matches ${base}.`}
        </span>
      </div>
    );
  }

  const others = othersCount(authorship);
  const atRisk = review.cost.queries_at_risk;

  return (
    <div className="bar">
      <span className="bic" aria-hidden="true">
        <Icon name="pencil" size={14} />
      </span>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--accent-ink)" }}>
          Staged in the Candidate — not on the cluster yet
        </div>
        <div className="mono" style={{ fontSize: 11.5, color: "var(--gray)", whiteSpace: "nowrap" }}>
          Based on {base} · {plural(counts.total, "change")}
          {others !== null && others > 0 && ` · ${others} by others`}
        </div>
      </div>
      <span className="vr" style={{ background: "var(--accent-bd)", height: 24 }} aria-hidden="true" />
      {counts.noRestart > 0 && (
        <span className="eff eg">
          <Icon name="bolt" size={13} width={2} />
          {counts.noRestart} apply without restart
        </span>
      )}
      {counts.noRestart > 0 && counts.rollout > 0 && (
        <span style={{ color: "#a9b3c1" }} aria-hidden="true">
          ·
        </span>
      )}
      {review.cost.restarts_coordinator && (
        <span className="eff ea">
          <Icon name="rollout" size={13} width={2} />
          {counts.rollout > 0
            ? `${counts.rollout} need a Rollout`
            : "The Apply needs a Rollout"}
        </span>
      )}
      {review.cost.restarts_coordinator && atRisk != null && (
        <Pill tone="r" title="Read from the coordinator just now. Already stale.">
          {plural(atRisk, "query", "queries")} at risk
        </Pill>
      )}
      <span style={{ display: "flex", flexWrap: "wrap", gap: 8, marginLeft: "auto" }}>
        <Link className="btn" to="/review">
          Review changes
        </Link>
        <button type="button" className="btn" onClick={() => setConfirmReset(true)}>
          Reset
        </button>
        <button
          type="button"
          className="btn"
          disabled={validate.isPending}
          onClick={() =>
            validate.mutate(undefined, {
              onSuccess: (v) => navigate(`/validations?id=${v.id}`),
            })
          }
        >
          {validate.isPending ? "Starting…" : "Validate"}
        </button>
        <Link className="btn pri" to="/apply">
          <Icon name="check" size={14} width={2} />
          Apply…
        </Link>
      </span>
      {validate.isError && <ErrorNote error={validate.error} />}

      <Dialog
        open={confirmReset}
        onClose={() => setConfirmReset(false)}
        title="Reset everything staged?"
        actions={
          <>
            <button type="button" className="btn" onClick={() => setConfirmReset(false)}>
              Keep the changes
            </button>
            <button
              type="button"
              className="btn dng-solid"
              disabled={reset.isPending}
              onClick={() => reset.mutate(undefined, { onSuccess: () => setConfirmReset(false) })}
            >
              Reset {plural(counts.total, "change")}
            </button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          The Candidate goes back to {base}. All {plural(counts.total, "staged change")} {counts.total === 1 ? "is" : "are"}{" "}
          discarded
          {others !== null && others > 0 ? `, including ${others} staged by other people` : ""}.
        </p>
        <p className="sm muted" style={{ margin: 0 }}>
          Nothing reaches the cluster: Reset only throws staged work away. To undo one change,
          use its Reset in Review instead.
        </p>
        <ErrorNote error={reset.error} />
      </Dialog>
    </div>
  );
}

function Item({
  to,
  icon,
  label,
  end,
  children,
}: {
  to: string;
  icon: IconName;
  label: string;
  end?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `ni${isActive ? " on" : ""}`}>
      <Icon name={icon} />
      <span className="lb">{label}</span>
      {children}
    </NavLink>
  );
}

function Sidebar({
  review,
  admin,
  enforced,
}: {
  review: Review | undefined;
  admin: boolean;
  enforced: boolean | undefined;
}) {
  const changed = new Map(
    (review?.sections ?? []).map((s) => [s.section, s.changes?.length ?? 0]),
  );
  const rollout = new Set(review?.cost.rollout_sections ?? []);
  const total = [...changed.values()].reduce((a, b) => a + b, 0);

  return (
    <nav className="side" aria-label="Main">
      <div style={{ paddingTop: 6 }}>
        <Item to="/" end icon="overview" label="Overview" />
        <Item to="/review" icon="candidate" label="Candidate">
          {total > 0 && <span className="bdg">{total}</span>}
        </Item>
      </div>
      <div className="gl">Configuration</div>
      {SECTIONS.map((s) => {
        const n = changed.get(s.key) ?? 0;
        return (
          <Item key={s.key} to={s.path} icon={s.icon} label={s.label}>
            {rollout.has(s.key) && (
              <Icon name="rollout" size={12} width={2.2} color="#9a5b06" label="Rollout" />
            )}
            {n > 0 && <span className="bdg">{n}</span>}
          </Item>
        );
      })}
      <div className="gl">Operations</div>
      <Item to="/snapshots" icon="clock" label="Snapshots" />
      <Item to="/drift" icon="pulse" label="Drift" />
      <Item to="/applies" icon="play" label="Applies" />
      <Item to="/validations" icon="checkCircle" label="Validations" />
      <Item to="/audit" icon="list" label="Audit" />
      <Item to="/import-export" icon="transfer" label="Import / export" />
      {admin && (
        <>
          <div className="gl">Admin</div>
          <Item to="/admin" end icon="sliders" label="Admin values" />
          <Item to="/admin/enforcement" icon="lock" label="Permission enforcement">
            {enforced !== undefined && (
              <Pill tone={enforced ? "g" : "x"} xs>
                {enforced ? "On" : "Off"}
              </Pill>
            )}
          </Item>
          <Item to="/admin/raw-files" icon="file" label="Raw files" />
          <Item to="/admin/adoption" icon="download" label="Adoption" />
        </>
      )}
      {admin ? (
        <Item to="/maintenance" icon="wrench" label="Maintenance Mode" />
      ) : (
        <>
          <div className="gl">Platform</div>
          <Item to="/maintenance" icon="wrench" label="Maintenance Mode" />
        </>
      )}
      <div className="gl">Developer</div>
      <Item to="/tokens" icon="key" label="API tokens" />
    </nav>
  );
}
