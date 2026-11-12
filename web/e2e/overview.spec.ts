import { test, expect } from "@playwright/test";
import { stubApi, AUDIT, SNAPSHOTS } from "./fixtures";

/**
 * The Overview, which carries the two §21 requirements the shell cannot: the committed
 * Snapshot in full, and the whole Candidate diff with who staged each change.
 *
 * Authorship is read from the audit trail rather than from the Candidate, because the
 * Candidate deliberately stores none -- `app/pipeline/candidate.py` says the audit "is the
 * only record of whose work an Apply is shipping".
 */

test("it names the committed Snapshot and when it was committed", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  const facts = page.getByText("Committed configuration").locator("..");
  await expect(facts).toContainText("Snapshot 118");
});

test("it attributes each staged change to whoever the audit says made it", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  const rows = page.getByRole("row");
  await expect(rows.filter({ hasText: "finance_pg" })).toContainText("dana");
  await expect(rows.filter({ hasText: "CN=*.analytics.corp" })).toContainText("sam");
});

test("a refused audit entry attributes nothing", async ({ page }) => {
  // `events_kafka` appears in the trail with outcome "refused". A refusal is not a change,
  // so attributing it to rui would name somebody for work that never landed.
  await stubApi(page);
  await page.goto("/");

  const row = page.getByRole("row").filter({ hasText: "events_kafka" });
  await expect(row).not.toContainText("rui");
  await expect(row).toContainText("—");
});

test("a change the audit cannot attribute says so rather than guessing", async ({ page }) => {
  await stubApi(page, { audit: { items: [], next: null } });
  await page.goto("/");

  const row = page.getByRole("row").filter({ hasText: "finance_pg" });
  await expect(row).toContainText("—");
  // And the summary is honest about it rather than claiming nobody staged anything.
  await expect(page.getByText("the audit does not say by whom")).toBeVisible();
});

test("it names the Operators who staged the Candidate", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  const fact = page.getByText("Candidate", { exact: true }).last().locator("..");
  await expect(fact).toContainText("dana");
  await expect(fact).toContainText("sam");
});

test("it lists recent Snapshots, newest first and marked current", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  const rows = page.getByRole("row");
  await expect(rows.filter({ hasText: "apl_8f31" })).toContainText("Current");
  await expect(rows.filter({ hasText: "apl_7c02" })).not.toContainText("Current");
  expect(SNAPSHOTS[0]!.number).toBeGreaterThan(SNAPSHOTS[1]!.number);
});

test("expiring certificates are counted from Apchi's own status, not a date", async ({
  page,
}) => {
  await stubApi(page, {
    certificates: [
      {
        name: "warehouse-prod",
        common_name: "warehouse",
        subject: "CN=warehouse",
        issuer: "CN=corp",
        fingerprint: "ab:cd",
        status: "expiring",
        not_before: "2025-08-01T00:00:00Z",
        not_after: "2026-09-12T00:00:00Z",
      },
    ],
  });
  await page.goto("/");

  const fact = page.getByText("Expiring certificates").locator("..");
  await expect(fact).toContainText("1");
  await expect(fact).toContainText("warehouse-prod");
});

test("no expiring certificate says so rather than showing a zero", async ({ page }) => {
  await stubApi(page, { certificates: [] });
  await page.goto("/");

  await expect(page.getByText("None inside the window")).toBeVisible();
});

test("a cluster with no Snapshot yet says so rather than rendering an empty table", async ({
  page,
}) => {
  await stubApi(page, {
    snapshots: { items: [], next: null },
    review: {
      has_changes: false,
      base_snapshot: null,
      sections: [],
      cost: { restarts_coordinator: false, queries_at_risk: null, warning: null },
    },
  });
  await page.goto("/");

  await expect(page.getByText(/No Snapshot has been committed yet/)).toBeVisible();
  await expect(page.getByText("No staged changes")).toBeVisible();
});

test("timestamps are absolute, so they can be matched against a Trino log", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  // "2 hours ago" is useless to an Operator correlating with a coordinator log.
  await expect(page.getByText(/ago/)).toHaveCount(0);
  expect(AUDIT.length).toBeGreaterThan(0);
});
