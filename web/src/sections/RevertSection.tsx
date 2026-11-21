import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, unwrap, type S, type SectionName } from "../api/client";
import { useInvalidateCandidate, useSnapshots } from "../api/queries";
import { Dialog } from "../ui/Dialog";
import { plural } from "../ui/format";
import { CostPill } from "../ui/Pill";
import { ErrorNote } from "../ui/States";
import { section } from "./index";

type RevertPath =
  | "/api/v1/catalogs/revert"
  | "/api/v1/certificates/revert"
  | "/api/v1/certificate-mapping/revert"
  | "/api/v1/permissions/revert"
  | "/api/v1/resource-groups/revert"
  | "/api/v1/event-listeners/revert";

/**
 * Section Revert: one Section back to an older Snapshot, every other left where it is.
 *
 * Always previewed first with `dry_run`, which is Apchi's own answer rather than a diff
 * this page computes -- so the preview and the act cannot disagree. The combination it
 * stages has never run anywhere, and the dialog says so before the button that stages it.
 */
export function RevertSection({ name, open, onClose }: { name: SectionName; open: boolean; onClose: () => void }) {
  const meta = section(name);
  const path = `/api/v1/${meta.api}/revert` as RevertPath;
  const snapshots = useSnapshots(20);
  const invalidate = useInvalidateCandidate();
  const [picked, setPicked] = useState<number | null>(null);
  const call = (dry_run: boolean) =>
    api.POST(path, { body: { snapshot: picked!, dry_run } }).then(unwrap);
  const preview = useMutation({ mutationFn: () => call(true) });
  const stage = useMutation({
    mutationFn: () => call(false),
    onSuccess: async () => {
      await invalidate();
      close();
    },
  });
  const close = () => {
    setPicked(null);
    preview.reset();
    stage.reset();
    onClose();
  };
  const items = snapshots.data?.items ?? [];
  const effect = preview.data;

  return (
    <Dialog
      open={open}
      onClose={close}
      title={`Revert ${meta.label} to an older Snapshot`}
      actions={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          {effect === undefined ? (
            <button type="button" className="btn pri" disabled={picked === null || preview.isPending} onClick={() => preview.mutate()}>
              {preview.isPending ? "Asking Apchi…" : "Preview"}
            </button>
          ) : (
            <button type="button" className="btn pri" disabled={stage.isPending} onClick={() => stage.mutate()}>
              {stage.isPending ? "Staging…" : "Stage revert"}
            </button>
          )}
        </>
      }
    >
      <label className="fl">
        Snapshot
        <select
          className="inp"
          value={picked ?? ""}
          onChange={(e) => {
            setPicked(e.target.value === "" ? null : Number(e.target.value));
            preview.reset();
          }}
        >
          <option value="">Choose a Snapshot…</option>
          {items.map((s) => (
            <option key={s.number} value={s.number}>
              Snapshot {s.number}
            </option>
          ))}
        </select>
      </label>
      {effect !== undefined && <Effect effect={effect} sectionLabel={meta.label} />}
      <ErrorNote error={preview.error ?? stage.error} />
    </Dialog>
  );
}

export function Effect({ effect, sectionLabel }: { effect: S["RevertEffect"]; sectionLabel?: string }) {
  const dropped = effect.catalogs_dropped ?? [];
  const created = effect.catalogs_created ?? [];
  const replaced = effect.catalogs_replaced ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div>{effect.summary}</div>
      {effect.other_sections_stay_at != null && (
        <div className="note note--amber note--flush">
          {sectionLabel ?? "This Section"} will be restored to Snapshot {effect.from_snapshot}; every other Section
          stays at Snapshot {effect.other_sections_stay_at}. <b>This combination has never existed, been validated or
          run.</b> It is staged into the Candidate and needs a normal Apply — it is not a return to a known-good state.
        </div>
      )}
      {dropped.length > 0 && (
        <Row label="Would be dropped" tone="r" names={dropped} after="Queries using these will fail" />
      )}
      {created.length > 0 && <Row label="Would be created" tone="g" names={created} />}
      {replaced.length > 0 && <Row label="Would be replaced" tone="b" names={replaced} />}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <CostPill rollout={effect.cost.restarts_coordinator} />
        {effect.cost.restarts_coordinator && effect.cost.queries_at_risk != null && (
          <span className="sm" style={{ color: "var(--red)" }}>
            Running queries will be terminated · {plural(effect.cost.queries_at_risk, "query", "queries")} running now
          </span>
        )}
      </div>
      <span className="sm muted">
        This is Apchi's own answer from a dry run — exactly what staging would do, with nothing staged yet. A preview
        leaves no audit entry.
      </span>
    </div>
  );
}

function Row({ label, tone, names, after }: { label: string; tone: "r" | "g" | "b"; names: string[]; after?: string }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
      <span className="sm" style={{ minWidth: 120 }}>{label}</span>
      {names.map((n) => (
        <span key={n} className={`pill ${tone} mono`}>
          {n}
        </span>
      ))}
      {after !== undefined && <span className="sm muted">{after}</span>}
    </div>
  );
}
