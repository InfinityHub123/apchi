import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, unwrap } from "../api/client";
import { useEnforcement } from "../admin/requests";
import { plural } from "../ui/format";
import { CostPill, Pill } from "../ui/Pill";
import { ErrorNote, Loading, PageHead } from "../ui/States";

/** Whether the grants in Permissions decide who can reach what. An Admin's decision. */
export function EnforcementScreen() {
  const enforcement = useEnforcement();
  const queries = useQueryClient();
  const grants = useQuery({ queryKey: ["section", "permissions"], queryFn: () => api.GET("/api/v1/permissions").then(unwrap) });
  const set = useMutation({
    mutationFn: (enforced: boolean) => api.PUT("/api/v1/admin/permissions/enforcement", { body: { enforced } }).then(unwrap),
    onSuccess: () => Promise.all([queries.invalidateQueries({ queryKey: ["admin"] }), queries.invalidateQueries({ queryKey: ["cluster"] })]),
  });
  const on = enforcement.data?.enforced;
  const identities = new Set((grants.data ?? []).map((g) => g.identity)).size;

  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <PageHead
        crumb={<Link to="/admin">Admin values</Link>}
        title={
          <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
            Permission enforcement {on !== undefined && <Pill tone={on ? "g" : "x"}>{on ? "On" : "Off"}</Pill>} <CostPill rollout={false} />
          </span>
        }
        sub="Whether the grants in Permissions decide who can reach what"
      />
      <ErrorNote error={enforcement.error ?? set.error} />
      {on === undefined ? (
        !enforcement.error && <Loading what="enforcement" />
      ) : (
        <>
          <div className="row">
            <section className="card cp" style={{ flex: "1 1 340px", borderColor: on ? undefined : "var(--accent-bd)" }}>
              <div className="lab">Off {on ? "" : "— now"}</div>
              <p>The generated rules end in a catch-all that allows everything to everyone — what the cluster did before Apchi.</p>
              <p className="muted">The {plural(grants.data?.length ?? 0, "grant")} in Permissions are configuration that nothing acts on. They are staged, reviewed and applied, but they restrict nobody.</p>
            </section>
            <section className="card cp" style={{ flex: "1 1 340px", borderColor: on ? "var(--accent-bd)" : undefined }}>
              <div className="lab">On {on ? "— now" : "— after the next Apply"}</div>
              <p>
                The catch-all is removed. A Trino Identity reaches only what it has been <b>granted</b>, plus anything a preserved
                access-control rule still allows.
              </p>
              <p style={{ color: "var(--red)" }}>On a cluster already serving queries, every identity without a grant loses everything it had.</p>
            </section>
          </div>
          <section className="card">
            <div className="chd">
              <span className="ct">Before turning it on</span>
            </div>
            <div className="cp kv" style={{ gridTemplateColumns: "1fr auto" }}>
              <span className="k">Trino Identities with at least one grant</span>
              <span className="v mono">{grants.data === undefined ? "…" : identities}</span>
              <span className="k">Identities seen in queries that have no grant</span>
              <span className="v muted">not known — Apchi cannot see query traffic</span>
            </div>
            <div className="note">
              This is an Admin's decision and an Admin's timing — Operators can't change it, because they would be closing the cluster by
              editing configuration. Changing it stages nothing in the Candidate: it is delivered by the next Apply, and costs no restart
              because Trino re-reads the rules on its refresh timer.
            </div>
            <div className="cft">
              {!on && <span className="sm" style={{ color: "var(--red)" }}>Identities without a grant will lose access</span>}
              <span className="sp" />
              <button type="button" className={`btn ${on ? "" : "dng-solid"}`} disabled={set.isPending} onClick={() => set.mutate(!on)}>
                {on ? "Turn enforcement off" : "Turn enforcement on"}
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
