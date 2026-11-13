import { test, expect } from "@playwright/test";
import { stubApi, REVIEW_CLEAN, REVIEW_STAGED } from "./fixtures";

/**
 * §21's requirements on the shell, tested as behaviour rather than as markup.
 *
 * Each of these exists because the spec names it, and would catch a redesign that quietly
 * dropped it -- which is the way a UI loses a safety property nobody notices is gone.
 */

test("the shell names the cluster, the Snapshot and who is looking", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  await expect(page.getByRole("banner")).toContainText("Apchi");
  await expect(page.getByRole("banner")).toContainText("Healthy");
  await expect(page.getByRole("banner")).toContainText("dana");
  await expect(page.getByText("based on Snapshot 118")).toBeVisible();
});

test("a staged Candidate says so, and says how many changes", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  // Scoped to the strip: the Overview reports the same count, and an unscoped match would
  // pass while the strip said nothing.
  await expect(candidate(page)).toContainText("4 changes");
  await expect(candidate(page)).toContainText("based on Snapshot 118");
  // The point of the strip: staged is not live, and the work area says so too.
  await expect(page.getByText(/Nothing below has reached the cluster/)).toBeVisible();
});

test("a Candidate that forces a rollout carries the live query count", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  await expect(page.getByText("Rollout").first()).toBeVisible();
  await expect(page.getByText("47 queries at risk")).toBeVisible();
});

test("a Candidate with no rollout shows no query count", async ({ page }) => {
  await stubApi(page, {
    review: {
      ...REVIEW_STAGED,
      sections: [REVIEW_STAGED.sections[0]],
      cost: { restarts_coordinator: false, queries_at_risk: null, warning: null },
    },
  });
  await page.goto("/");

  await expect(page.getByText("No restart").first()).toBeVisible();
  await expect(page.getByText(/queries at risk/)).toHaveCount(0);
});

test("the review table marks each change's cost per Section, not just overall", async ({
  page,
}) => {
  await stubApi(page);
  await page.goto("/");

  const rows = page.getByRole("row");
  // Catalogs apply by DDL; certificate mapping is read at start-up. The table has to tell
  // them apart, which is the §21 requirement an aggregate cost cannot satisfy.
  await expect(rows.filter({ hasText: "finance_pg" })).toContainText("No restart");
  await expect(rows.filter({ hasText: "CN=*.analytics.corp" })).toContainText("Rollout");
});

test("a change names what happened to it for a screen reader, not only in colour", async ({
  page,
}) => {
  await stubApi(page);
  await page.goto("/");

  const removed = page.getByRole("row").filter({ hasText: "legacy_mysql" });
  await expect(removed).toContainText("removed");
});

test("an empty Candidate says there is nothing staged and offers no Apply", async ({ page }) => {
  await stubApi(page, { review: REVIEW_CLEAN });
  await page.goto("/");

  await expect(page.getByText("No staged changes")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply" })).toHaveCount(0);
  await expect(page.getByText(/No Section has staged changes/)).toBeVisible();
});

test("an unhealthy cluster carries §21's verification wording", async ({ page }) => {
  await stubApi(page, { health: { status: "degraded", mongo: "unreachable" } });
  await page.goto("/");

  const alert = page.getByRole("alert");
  await expect(alert).toContainText("did not pass health verification");
  await expect(alert).toContainText("previous Snapshot remains the known-good configuration");
});


test("a failed review is reported, not rendered as empty", async ({ page }) => {
  await stubApi(page);
  // Registered after stubApi, because Playwright matches the most recently added route.
  await page.route("**/api/v1/review", (r) =>
    r.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "MongoDB is unreachable" }),
    }),
  );
  await page.goto("/");

  await expect(page.getByText("MongoDB is unreachable")).toBeVisible();
});

test("every interactive control is reachable by keyboard", async ({ page }) => {
  await stubApi(page);
  await page.goto("/");

  // Wait for the strip before tabbing: a Tab pressed into a half-rendered page lands
  // nowhere and the walk below starts from the wrong place.
  await expect(page.getByRole("button", { name: "Apply" })).toBeVisible();

  const reachable: string[] = [];
  for (let i = 0; i < 25; i += 1) {
    await page.keyboard.press("Tab");
    const label = await page.evaluate(() => {
      const el = document.activeElement;
      if (el === null || el === document.body) return null;
      return `${el.tagName.toLowerCase()}:${(el.textContent ?? "").trim().slice(0, 24)}`;
    });
    if (label !== null && !reachable.includes(label)) reachable.push(label);
  }

  expect(reachable).toContain("button:Reset");
  expect(reachable).toContain("button:Validate");
  expect(reachable).toContain("button:Apply");
});

/** The Candidate strip, which every screen carries and more than one place names. */
function candidate(page: import("@playwright/test").Page) {
  return page.getByRole("region", { name: "Candidate" });
}
