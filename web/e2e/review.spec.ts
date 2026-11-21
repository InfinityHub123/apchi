import { test, expect, type Route } from "@playwright/test";
import { AUDIT, CLUSTER, DRIFT, REVIEW_STAGED, stubApi } from "./fixtures";

/** The Staged changes screen: every person's work, what it costs, and undoing one of it. */

test("each change names its author from the audit, or says it does not know", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");

  await expect(page.getByRole("region", { name: "Catalog finance_pg" })).toContainText("staged by jo");
  await expect(page.getByRole("region", { name: "Resource groups global.etl.batch" })).toContainText("staged by you");
  const kafka = page.getByRole("region", { name: "Event listener events_kafka" });
  await expect(kafka).toContainText("author not known");
  await expect(kafka).toContainText("Apchi does not guess");
});

test("a change that arrived with a Section Revert is credited to the revert", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/audit": {
      items: [
        { ...AUDIT[3], id: "z1", at: "2026-11-17T16:00:00Z", actor: "mira", action: "reverted", resource: null },
        ...AUDIT,
      ],
      next: null,
    },
  });
  await page.goto("/review");
  await expect(page.getByRole("region", { name: "Catalog finance_pg" })).toContainText("staged by a Section Revert by mira");
});

test("a secret is masked until shown, and only the fields that moved are listed", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");
  const card = page.getByRole("region", { name: "Catalog finance_pg" });
  await expect(card).toContainText("connection-url");
  await expect(card).not.toContainText("hunter2");
  await card.getByRole("button", { name: "Show" }).click();
  await expect(card).toContainText("hunter2");
});

test("Reset on one change sends just that resource", async ({ page }) => {
  const sent: unknown[] = [];
  await stubApi(page, {
    "POST /api/v1/candidate/reset-resource": (route: Route) => {
      sent.push(route.request().postDataJSON());
      return null;
    },
  });
  await page.goto("/review");
  await page.getByRole("region", { name: "Catalog marketing_mysql" }).getByRole("button", { name: "Reset" }).click();
  await expect.poll(() => sent).toEqual([{ section: "catalogs", resource: "marketing_mysql" }]);
});

test("the Rollout card names a Section that is in it with nothing staged", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");
  await expect(page.getByText("The Rollout is for 3 Sections")).toBeVisible();
  await expect(page.getByText("Nothing is staged here.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Check drift" })).toBeVisible();
});

test("the filters narrow to Rollout changes and to the caller's own", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");
  await page.getByRole("button", { name: /^Rollout/ }).click();
  await expect(page.getByRole("region", { name: /^Catalog / })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Event listener events_kafka" })).toBeVisible();
  await page.getByRole("button", { name: /^Staged by you/ }).click();
  await expect(page.getByRole("region", { name: "Resource groups global.etl.batch" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Event listener events_kafka" })).toHaveCount(0);
});

test("the Candidate's size is shown against the limit the API reports", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");
  await expect(page.getByText("412 KB of 15.0 MB")).toBeVisible();
});

test("the last Validation says how often the Candidate changed since", async ({ page }) => {
  await stubApi(page);
  await page.goto("/review");
  // The Validation started at 11:00; mira and dana staged three changes after it.
  await expect(page.getByText(/Last Validation passed/)).toBeVisible();
  await expect(page.getByText("the Candidate changed 3 times since")).toBeVisible();
});

test("a frozen Candidate disables per-row Reset", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/cluster": { ...CLUSTER, last_apply: { ...CLUSTER.last_apply, id: "a-9", stage: "rolling_out", finished_at: null } },
  });
  await page.goto("/review");
  await expect(page.getByRole("region", { name: "Catalog finance_pg" }).getByRole("button", { name: "Reset" })).toBeDisabled();
  await expect(page.getByText("Candidate frozen")).toBeVisible();
});

test("the Overview opens a change to show which field moved", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");
  await expect(page.getByText("Snapshot 13").first()).toBeVisible();
  await page.getByRole("button", { name: /finance_pg/ }).click();
  await expect(page.getByRole("cell", { name: "properties.connection-url", exact: true })).toBeVisible();
  await expect(page.getByText("search_tls")).toBeVisible();
  await expect(page.getByText(/staged by jo, mira and you|staged by .*you/)).toBeVisible();
});

test("drift says which Section diverged and what reconciling it costs", async ({ page }) => {
  await stubApi(page);
  await page.goto("/drift");
  await expect(page.getByText("1 Section no longer matches Snapshot 13")).toBeVisible();
  const row = page.getByRole("row", { name: /Certificate mapping/ });
  await expect(row).toContainText("Diverged");
  await expect(row).toContainText("Rollout");
  await expect(page.getByRole("row", { name: /Catalogs/ })).toContainText("Not comparable");
});

test("drift with an unknown Section is not confirmed, not in sync", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/cluster/drift": {
      ...DRIFT,
      diverged: null,
      sections: DRIFT.sections.map((s) => (s.status === "diverged" ? { ...s, status: "in_sync" } : s)),
    },
  });
  await page.goto("/drift");
  await expect(page.getByText("Not confirmed").first()).toBeVisible();
});

test("an empty Candidate's Review says so", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/review": { ...REVIEW_STAGED, has_changes: false, sections: [], cost: { ...REVIEW_STAGED.cost, rollout_sections: [], restarts_coordinator: false } },
  });
  await page.goto("/review");
  await expect(page.getByText("Nothing is staged.")).toBeVisible();
});
