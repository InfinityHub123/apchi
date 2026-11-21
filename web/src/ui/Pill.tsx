import type { ReactNode } from "react";
import { Icon } from "./Icon";

/**
 * Every state in this interface is a pill, and every pill is one of five tones.
 *
 * The tones are semantic, not decorative: green is a good or harmless state, amber is a cost
 * or a warning (above all, a Rollout), red is a failure or a destructive consequence, blue is
 * the Candidate -- staged, not live -- and gray is neutral. A sixth tone, or a tone used for
 * emphasis, breaks the reading an Operator learned on the first screen.
 */
export type Tone = "g" | "a" | "r" | "b" | "x";

export function Pill({
  tone = "x",
  dot = false,
  xs = false,
  title,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  xs?: boolean;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span className={`pill ${tone}${xs ? " pill--xs" : ""}`} title={title}>
      {dot && <span className="d" aria-hidden="true" />}
      {children}
    </span>
  );
}

/**
 * What a change costs the cluster. Only ever read from the API's `rollout_sections` or a
 * Section's own `restarts_coordinator`, never from a table here: a Section is also in the
 * Rollout when the cluster drifted from what Apchi would write, which no static map knows.
 */
export function CostPill({ rollout }: { rollout: boolean }) {
  return rollout ? (
    <Pill tone="a">
      <Icon name="rollout" size={11} width={2.2} />
      Rollout
    </Pill>
  ) : (
    <Pill tone="g">
      <Icon name="bolt" size={11} width={2.2} />
      No restart
    </Pill>
  );
}

export type ChangeKind = "added" | "changed" | "removed";

const CHANGE: Record<ChangeKind, string> = {
  added: "Added",
  changed: "Modified",
  removed: "Removed",
};

/** Staged changes are blue: the Candidate's colour, so "staged" never reads as "live". */
export function ChangePill({ change, staged = true }: { change: ChangeKind; staged?: boolean }) {
  return (
    <Pill tone={change === "removed" ? "r" : "b"}>
      {CHANGE[change]}
      {staged && " · staged"}
    </Pill>
  );
}
