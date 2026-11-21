import { test, expect } from "@playwright/test";
import { CLUSTER, REVIEW_CLEAN, Reply, stubApi } from "./fixtures";

/**
 * §21's requirements on the shell, tested as behaviour rather than as markup.
 *
 * Each exists because the spec names it, and would catch a redesign that quietly dropped
 * it -- the way a UI loses a safety property nobody notices is gone.
 */

test("the top bar names the live Snapshot, the coordinator and who is looking", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  const top = page.getByRole("banner");
  await expect(top).toContainText("Snapshot 13");
  await expect(top).toContainText("verified at its Apply");
  await expect(top).toContainText("Coordinator up · 8 workers");
  await expect(top).toContainText("dana");
  await expect(top).toContainText("Admin · SSO");
  await expect(top).toContainText("Maintenance Mode off");
});

test("the coordinator is read on demand, not on a timer", async ({ page }) => {
  let probes = 0;
  await stubApi(page, {
    "GET /api/v1/cluster/probe": () => {
      probes++;
      return { at: new Date().toISOString(), coordinator: "starting", workers: null };
    },
  });
  await page.goto("/");
  await expect(page.getByText("Coordinator starting")).toBeVisible();
  await page.waitForTimeout(1500);
  expect(probes).toBe(1);

  await page.getByRole("button", { name: "Check again" }).click();
  await expect.poll(() => probes).toBe(2);
});

test("a staged Candidate says so from every screen, with what it costs", async ({ page }) => {
  await stubApi(page);
  await page.goto("/snapshots");

  await expect(page.getByText("Staged in the Candidate — not on the cluster yet")).toBeVisible();
  await expect(page.getByText(/Based on Snapshot 13 · 5 changes/)).toBeVisible();
  // Three catalog changes ride no restart; the resource-group and listener changes do.
  await expect(page.getByText("3 apply without restart")).toBeVisible();
  await expect(page.getByText("2 need a Rollout")).toBeVisible();
  await expect(page.getByText("14 queries at risk")).toBeVisible();
});

test("the bar counts the changes staged by other people, from the audit", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");
  // dana staged one; jo and mira three; events_kafka has no author on the trail.
  await expect(page.getByText(/5 changes · 3 by others/)).toBeVisible();
});

test("a clean Candidate says it matches the Snapshot", async ({ page }) => {
  await stubApi(page, { "GET /api/v1/review": REVIEW_CLEAN });
  await page.goto("/");
  await expect(page.getByText("Nothing staged")).toBeVisible();
  await expect(page.getByText("The Candidate matches Snapshot 13.")).toBeVisible();
});

test("the sidebar marks Sections in the Rollout from the API, not from a table", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Main" });
  // Certificate mapping has nothing staged and is in the Rollout anyway: drift.
  const mapping = nav.getByRole("link", { name: /Certificate mapping/ });
  await expect(mapping.getByRole("img", { name: "Rollout" })).toBeVisible();
  await expect(nav.getByRole("link", { name: /Catalogs/ })).toContainText("3");
  await expect(nav.getByRole("link", { name: /^Catalogs/ }).getByRole("img", { name: "Rollout" })).toHaveCount(0);
});

test("Reset asks first, names what it throws away, then resets", async ({ page }) => {
  let resets = 0;
  await stubApi(page, {
    "POST /api/v1/candidate/reset": () => {
      resets++;
      return null;
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Reset", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("including 3 staged by other people");
  expect(resets).toBe(0);
  await dialog.getByRole("button", { name: "Reset 5 changes" }).click();
  await expect.poll(() => resets).toBe(1);
});

test("Maintenance Mode locks the Candidate's actions", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/cluster": {
      ...CLUSTER,
      maintenance: { engaged: true, engaged_at: "2026-11-17T15:40:47Z", engaged_by: "admin", reason: "Upgrade" },
    },
  });
  await page.goto("/");
  await expect(page.getByText("Candidate locked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply" })).toBeDisabled();
  await expect(page.getByRole("banner")).toContainText("Maintenance Mode on");
});

test("an incident takes over the top of every screen with §21's sentence", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/cluster": {
      ...CLUSTER,
      maintenance: { engaged: true, engaged_at: "2026-11-17T15:40:47Z", engaged_by: "apchi", reason: "Auto Rollback failed" },
      last_apply: { ...CLUSTER.last_apply, id: "a-8e21", stage: "incident", verified: false, snapshot: null },
    },
    "GET /api/v1/applies/*": { id: "a-8e21", stage: "incident", operator_message: "Apply a-8e21 failed and its Auto Rollback failed too." },
  });
  await page.goto("/");
  await expect(page.getByRole("alert").first()).toContainText(
    "The Trino cluster is currently unhealthy after applying configuration. Please contact our team.",
  );
  await expect(page.getByRole("banner")).toContainText("Incident · configuration unknown");
  await expect(page.getByRole("link", { name: "View diagnostic" })).toHaveAttribute("href", "/applies/a-8e21");
});

test("an Operator does not see the Admin screens", async ({ page }) => {
  await stubApi(page, { "GET /api/v1/auth/me": { subject: "jo", role: "operator", via: "password", token_id: null } });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: "Audit" })).toBeVisible();
  await expect(nav.getByRole("link", { name: /Raw files/ })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Maintenance Mode" })).toBeVisible();
});

test("a failed read is shown in Apchi's words", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/review": new Reply(503, { code: "unavailable", message: "MongoDB is not answering.", request_id: "r" }),
  });
  await page.goto("/");
  await expect(page.getByText("MongoDB is not answering.")).toBeVisible();
});

test("a deep link loads its screen", async ({ page }) => {
  await stubApi(page);
  await page.goto("/audit");
  await expect(page.getByRole("link", { name: "Audit" })).toHaveClass(/on/);
});
