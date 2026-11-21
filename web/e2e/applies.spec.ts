import { test, expect, type Route } from "@playwright/test";
import { CLUSTER, REVIEW_STAGED, Reply, VALIDATIONS, stubApi } from "./fixtures";

/** Confirming an Apply, following it, and reading what happened afterwards. */

const at = (m: number) => `2026-11-17T15:${String(22 + m).padStart(2, "0")}:31Z`;
const ev = (stage: string, m: number, detail: string | null = null) => ({ stage, at: at(m), detail });

const CHECKS = [
  { check: "coordinator_responding", outcome: "passed", detail: "answered" },
  { check: "workers_registered", outcome: "failed", detail: "0 of 8 workers registered within 600s" },
  { check: "smoke_query", outcome: "not_reached", detail: null },
  { check: "catalogs_adopted", outcome: "not_reached", detail: null },
];

const record = (over: Record<string, unknown>) => ({
  id: "apl_8e21",
  kind: "apply",
  started_by: "dana",
  started_via: "sso",
  stage: "succeeded",
  history: [ev("validating", 0), ev("applying", 2), ev("rolling_out", 2), ev("verifying", 6), ev("committing", 7), ev("succeeded", 7)],
  failure_reason: null,
  interrupted: false,
  rollback: null,
  operator_message: null,
  warnings: [],
  failures: [],
  verification: [],
  base_snapshot: 13,
  snapshot: 14,
  started_at: at(0),
  finished_at: at(7),
  rolled_out: true,
  ...over,
});

test("the confirmation says running queries will be terminated, in those words, with the count", async ({ page }) => {
  await stubApi(page);
  await page.goto("/apply");
  const confirm = page.getByRole("region", { name: "Confirm Apply" });
  await expect(confirm).toContainText("Running queries will be terminated.");
  await expect(confirm).toContainText("14");
  await expect(confirm).toContainText("a live reading, not a promise");
  await expect(confirm.getByRole("button", { name: "Apply and roll out" })).toBeEnabled();
  // Rollout changes are listed first, with who staged each.
  await expect(confirm.getByRole("row").nth(1)).toContainText("Rollout");
  await expect(confirm.getByRole("row", { name: /finance_pg/ })).toContainText("jo");
});

test("starting an Apply moves to its progress, and the URL can be reloaded", async ({ page }) => {
  let started = false;
  const running = record({
    stage: "rolling_out",
    history: [ev("validating", 0), ev("applying", 2), ev("rolling_out", 2)],
    snapshot: null,
    finished_at: null,
  });
  await stubApi(page, {
    "POST /api/v1/applies": () => {
      started = true;
      return running;
    },
    "GET /api/v1/applies/*": running,
    "GET /api/v1/applies/apl_8e21/events": (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: 'id: 0\nevent: stage\ndata: {"stage":"validating","at":"2026-11-17T15:22:31Z","detail":null}\n\n',
      }),
  });
  await page.goto("/apply");
  await page.getByRole("button", { name: "Apply and roll out" }).click();
  await expect.poll(() => started).toBe(true);
  await expect(page).toHaveURL(/\/apply\?id=apl_8e21/);
  await expect(page.getByRole("region", { name: "Rolling out: In progress" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Verification: Waiting" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("region", { name: "Rolling out: In progress" })).toBeVisible();
});

test("an Apply with nothing that restarts says no Rollout is needed", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/applies/*": record({
      history: [ev("validating", 0), ev("applying", 2), ev("verifying", 3), ev("committing", 3), ev("succeeded", 3)],
      rolled_out: false,
    }),
  });
  await page.goto("/applies/apl_8e21");
  await expect(page.getByRole("region", { name: "Rolling out: Not needed" })).toBeVisible();
  await expect(page.getByText("created Snapshot 14")).toBeVisible();
});

test("a failed Verification shows the fixed sentence, the checks and the diagnostic", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/applies/*": record({
      stage: "failed",
      history: [ev("validating", 0), ev("applying", 2), ev("rolling_out", 2), ev("verifying", 8), ev("rolling_back", 9), ev("failed", 12)],
      verification: CHECKS,
      rollback: "succeeded",
      failure_reason: "VerificationFailed: 0 of 8 workers registered within 600s",
      snapshot: null,
    }),
  });
  await page.goto("/applies/apl_8e21");
  await expect(
    page.getByText(
      "Configuration was applied, but the Trino cluster did not pass health verification. The previous Snapshot remains the known-good configuration. Please contact our team if the cluster does not recover.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("row", { name: /Workers registered/ })).toContainText("Failed");
  await expect(page.getByRole("row", { name: /Smoke query/ })).toContainText("Not reached");
  await expect(page.getByText('"Not reached" is not a pass')).toBeVisible();
  await expect(page.getByText("VerificationFailed: 0 of 8 workers")).toBeVisible();
  await expect(page.getByText("Put back", { exact: true })).toBeVisible();
});

test("a Validation failure names the Section that failed, not always Resource Groups", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/applies/*": record({
      stage: "failed",
      history: [ev("validating", 0), ev("failed", 3)],
      failures: [{ section: "catalogs", resource: "imaginary", reason: "'no-such-property' was not used" }],
      failure_reason: "ValidationFailed: imaginary",
      snapshot: null,
      rolled_out: false,
    }),
  });
  await page.goto("/applies/apl_8e21");
  await expect(page.getByText("Catalogs configuration could not be loaded by Trino. No changes were applied.")).toBeVisible();
  await expect(page.getByText(/imaginary — 'no-such-property' was not used/)).toBeVisible();
});

test("the incident page shows the stages, the checks and the release form for an Admin", async ({ page }) => {
  let released: unknown = null;
  await stubApi(page, {
    "GET /api/v1/cluster": {
      ...CLUSTER,
      maintenance: { engaged: true, engaged_at: at(18), engaged_by: "auto_rollback_failure", reason: "Auto Rollback failed" },
      last_apply: { ...CLUSTER.last_apply, id: "apl_8e21", stage: "incident", verified: false, snapshot: null },
    },
    "GET /api/v1/applies/*": record({
      stage: "incident",
      history: [ev("validating", 0), ev("applying", 2), ev("rolling_out", 2), ev("verifying", 8), ev("rolling_back", 9), ev("incident", 18)],
      verification: CHECKS,
      rollback: "failed",
      failure_reason: "verification failed. Auto Rollback then failed: coordinator never came back",
      operator_message: "The Trino cluster is currently unhealthy after applying configuration. Please contact our team.",
      snapshot: null,
    }),
    "PUT /api/v1/admin/maintenance-mode": (route: Route) => {
      released = route.request().postDataJSON();
      return { engaged: false, engaged_at: null, engaged_by: null, reason: null };
    },
  });
  await page.goto("/applies/apl_8e21");
  await expect(page.getByRole("heading", { name: /Incident · Apply apl_8e21/ })).toBeVisible();
  await expect(page.getByText("Rolling back to Snapshot 13")).toBeVisible();
  await expect(page.getByText("Apchi, automatically")).toBeVisible();
  const release = page.getByRole("button", { name: "Release Maintenance Mode" });
  await expect(release).toBeDisabled();
  await page.getByLabel("Reason for releasing").fill("node pool fixed, coordinator healthy");
  await release.click();
  await expect.poll(() => released).toEqual({ engaged: false, reason: "node pool fixed, coordinator healthy" });
});

test("the Applies list tells an Admin Apply and an interruption apart", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/applies": {
      items: [
        record({ id: "apl_a", kind: "admin_apply", snapshot: null }),
        record({ id: "apl_b", stage: "failed", interrupted: true, snapshot: null, history: [ev("validating", 0), ev("applying", 1), ev("failed", 2)] }),
        record({ id: "apl_c", started_by: "jo", started_via: "token" }),
      ],
      next: "cursor",
    },
  });
  await page.goto("/applies");
  await expect(page.getByRole("row", { name: /apl_a/ })).toContainText("Admin Apply");
  await expect(page.getByRole("row", { name: /apl_a/ })).toContainText("none — Admin Applies create no Snapshot");
  await expect(page.getByRole("row", { name: /apl_b/ })).toContainText("Interrupted · put back");
  await expect(page.getByRole("row", { name: /apl_c/ })).toContainText("jo via token");
  await expect(page.getByRole("button", { name: "Load older" })).toBeVisible();
});

test("a Validation's failures are all listed, and a later edit is pointed out", async ({ page }) => {
  const failed = {
    ...VALIDATIONS[0],
    id: "v-31a8",
    outcome: "failed",
    failures: [
      { section: "resource_groups", resource: "global.etl.batch", reason: "hardCpuLimit is set but cpuQuotaPeriod is not" },
      { section: "catalogs", resource: "marketing_mysql", reason: "Trino rejected property connection-urll" },
    ],
  };
  await stubApi(page, {
    "GET /api/v1/validations": { items: [failed], next: null },
    "GET /api/v1/validations/*": failed,
  });
  await page.goto("/validations");
  await expect(page.getByText("Resource groups and Catalogs configuration could not be loaded by Trino.")).toBeVisible();
  await expect(page.getByRole("row", { name: /global.etl.batch/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /marketing_mysql/ })).toBeVisible();
  await expect(page.getByText(/the Candidate changed 3 times/)).toBeVisible();
});

test("Validate from the bar starts one and opens it", async ({ page }) => {
  const running = { ...VALIDATIONS[0], id: "v-new", outcome: "running", finished_at: null };
  await stubApi(page, {
    "POST /api/v1/validations": running,
    "GET /api/v1/validations/*": running,
  });
  await page.goto("/review");
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page).toHaveURL(/\/validations\?id=v-new/);
  await expect(page.getByText(/Running\. A throwaway coordinator/)).toBeVisible();
});

test("an Apply refused by the API says why", async ({ page }) => {
  await stubApi(page, {
    "POST /api/v1/applies": new Reply(409, { code: "conflict", message: "Apply apl_x is already in flight.", request_id: "r" }),
    "GET /api/v1/review": { ...REVIEW_STAGED },
  });
  await page.goto("/apply");
  await page.getByRole("button", { name: "Apply and roll out" }).click();
  await expect(page.getByText("Apply apl_x is already in flight.")).toBeVisible();
});
