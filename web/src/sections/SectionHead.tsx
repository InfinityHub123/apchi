import { useState, type ReactNode } from "react";
import type { SectionName } from "../api/client";
import { CostPill } from "../ui/Pill";
import { PageHead } from "../ui/States";
import { section } from "./index";
import { RevertSection } from "./RevertSection";

/** The heading every Section screen shares: what it is, what it costs, and Revert Section. */
export function SectionHead({
  name,
  sub,
  rollout,
  view,
  onView,
  live,
  locked,
  children,
}: {
  name: SectionName;
  sub: ReactNode;
  rollout: boolean;
  view?: "candidate" | "live";
  onView?: (v: "candidate" | "live") => void;
  live?: number | null;
  locked: boolean;
  children?: ReactNode;
}) {
  const [revert, setRevert] = useState(false);
  return (
    <>
      <PageHead
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
            {section(name).label} <CostPill rollout={rollout} />
          </span>
        }
        sub={sub}
      >
        {onView !== undefined && live != null && (
          <span role="group" aria-label="View" style={{ display: "inline-flex", gap: 4 }}>
            <button type="button" className={`tc${view === "candidate" ? " sel" : ""}`} aria-pressed={view === "candidate"} onClick={() => onView("candidate")}>
              Candidate
            </button>
            <button type="button" className={`tc${view === "live" ? " sel" : ""}`} aria-pressed={view === "live"} onClick={() => onView("live")}>
              Live · Snapshot {live}
            </button>
          </span>
        )}
        <button type="button" className="btn" disabled={locked} onClick={() => setRevert(true)}>
          Revert Section…
        </button>
        {children}
      </PageHead>
      <RevertSection name={name} open={revert} onClose={() => setRevert(false)} />
    </>
  );
}
