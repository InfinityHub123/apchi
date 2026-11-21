import type { S } from "../api/client";
import { sectionLabel } from "../sections";
import type { Tone } from "../ui/Pill";

/**
 * What an Apply's end means, in the product's fixed words.
 *
 * §21 fixes the failure sentences as product copy. The Validation one names Resource Groups
 * because it was written against that example; it is used verbatim when Resource Groups is
 * the only Section that failed, and with the failing Sections named in its place otherwise --
 * telling somebody Resource Groups failed when a Catalog did would be the one rewording
 * worse than changing the sentence.
 */
export const APPLY_FAILED =
  "Configuration application failed. The previous configuration is being restored.";
export const VERIFICATION_FAILED =
  "Configuration was applied, but the Trino cluster did not pass health verification. The previous Snapshot remains the known-good configuration. Please contact our team if the cluster does not recover.";
export const INCIDENT =
  "The Trino cluster is currently unhealthy after applying configuration. Please contact our team.";

export function validationFailed(failures: S["ValidationFailure"][]): string {
  const sections = [...new Set(failures.flatMap((f) => (f.section == null ? [] : [f.section])))];
  if (sections.length === 0 || (sections.length === 1 && sections[0] === "resource_groups")) {
    return "Resource Groups configuration could not be loaded by Trino. No changes were applied.";
  }
  const named = sections.map(sectionLabel).join(", ").replace(/, ([^,]*)$/, " and $1");
  return `${named} configuration could not be loaded by Trino. No changes were applied.`;
}

const STAGE_LABEL: Record<string, string> = {
  validating: "Validating",
  applying: "Applying",
  rolling_out: "Rolling out",
  verifying: "Verifying",
  committing: "Committing",
  rolling_back: "Rolling back",
  succeeded: "Succeeded",
  failed: "Failed",
  incident: "Incident",
};
export const stageLabel = (s: string) => STAGE_LABEL[s] ?? s;

const reached = (r: S["ApplyRecord"], stage: string) => (r.history ?? []).some((e) => e.stage === stage);

export type Outcome = {
  tone: Tone;
  label: string;
  /** The fixed sentence, when the outcome has one. */
  message: string | null;
  inFlight: boolean;
};

export function outcomeOf(r: S["ApplyRecord"]): Outcome {
  if (r.finished_at == null && r.stage !== "succeeded" && r.stage !== "failed" && r.stage !== "incident") {
    return { tone: "b", label: stageLabel(r.stage), message: null, inFlight: true };
  }
  if (r.stage === "succeeded") return { tone: "g", label: "Succeeded", message: null, inFlight: false };
  if (r.stage === "incident") return { tone: "r", label: "Incident", message: INCIDENT, inFlight: false };
  if (r.interrupted) return { tone: "x", label: "Interrupted · put back", message: null, inFlight: false };
  // A failure before anything reached the cluster: Validation refused it.
  if (!reached(r, "applying")) {
    return { tone: "r", label: "Validation failed", message: validationFailed(r.failures ?? []), inFlight: false };
  }
  const verificationFailed = (r.verification ?? []).some((c) => c.outcome === "failed");
  return {
    tone: "r",
    label: r.rollback === "succeeded" ? "Failed · rolled back" : "Failed",
    message: verificationFailed ? VERIFICATION_FAILED : APPLY_FAILED,
    inFlight: false,
  };
}

const CHECK_LABEL: Record<string, string> = {
  coordinator_responding: "Coordinator responding",
  workers_registered: "Workers registered",
  smoke_query: "Smoke query",
};

/** "catalogs_adopted" → "Catalogs adopted"; the three fixed checks by name. */
export function checkLabel(check: string): string {
  if (CHECK_LABEL[check] !== undefined) return CHECK_LABEL[check]!;
  const m = /^(.*)_adopted$/.exec(check);
  if (m !== null) return `${sectionLabel(m[1] as S["SectionDiff"]["section"])} adopted`;
  return check.replace(/_/g, " ");
}
