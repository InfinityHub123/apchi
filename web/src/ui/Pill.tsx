import type { ReactNode } from "react";

/**
 * Every state in this interface is a pill, and every pill is one of five tones.
 *
 * The tones are semantic, not decorative: an Operator learns once that amber means a change
 * forces a coordinator rollout, and that reading holds on the review table, the Section rail
 * and the Apply confirmation. A sixth tone, or a tone used for emphasis, breaks that.
 */

export type Tone = "green" | "blue" | "amber" | "red" | "gray";

const TONE: Record<Tone, { fg: string; bg: string; bd: string }> = {
  green: { fg: "var(--green)", bg: "var(--green-bg)", bd: "var(--green-bd)" },
  blue: { fg: "var(--blue)", bg: "var(--blue-bg)", bd: "var(--blue-bd)" },
  amber: { fg: "var(--amber)", bg: "var(--amber-bg)", bd: "var(--amber-bd)" },
  red: { fg: "var(--red)", bg: "var(--red-bg)", bd: "var(--red-bd)" },
  gray: { fg: "var(--gray)", bg: "var(--gray-bg)", bd: "var(--gray-bd)" },
};

export function Pill({
  tone = "gray",
  dot = false,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  children: ReactNode;
}) {
  const c = TONE[tone];
  return (
    <span className="pill" style={{ color: c.fg, background: c.bg, borderColor: c.bd }}>
      {dot && <span className="pill__dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

/**
 * What one change costs the cluster, in the only two values it can have.
 *
 * Which Sections force a rollout is a fact about Trino, not a UI choice -- see
 * `sections.ts`, which also records why that table lives in the frontend at all.
 */
export function CostPill({ restarts }: { restarts: boolean }) {
  return restarts ? <Pill tone="amber">Rollout</Pill> : <Pill tone="green">No restart</Pill>;
}
