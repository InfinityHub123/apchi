import { test, expect, type Route } from "@playwright/test";
import { CLUSTER, GRANTS, REVIEW_STAGED, SELECTORS, stubApi } from "./fixtures";

/** Permissions and Resource groups. */

test("permissions say plainly that grants do nothing while enforcement is off", async ({ page }) => {
  await stubApi(page);
  await page.goto("/permissions");
  await expect(page.getByText(/Enforcement is off\./)).toBeVisible();
  await expect(page.getByRole("link", { name: "Manage enforcement" })).toBeVisible();
  await expect(page.getByText("Apchi's verification user may query the system catalog")).toBeVisible();

  await stubApi(page, { "GET /api/v1/cluster": { ...CLUSTER, permissions_enforced: true } });
  await page.reload();
  await expect(page.getByText(/Enforcement is off\./)).toHaveCount(0);
});

test("a modified grant shows which privileges it gains and loses", async ({ page }) => {
  const key = GRANTS[0]!.key;
  await stubApi(page, {
    "GET /api/v1/permissions": [{ ...GRANTS[0], privileges: ["SELECT"] }, ...GRANTS.slice(1)],
    "GET /api/v1/review": {
      ...REVIEW_STAGED,
      sections: [
        ...REVIEW_STAGED.sections.filter((s) => s.section !== "permissions"),
        {
          section: "permissions",
          changes: [{ resource: key, change: "changed", before: { ...GRANTS[0], privileges: ["SELECT", "GRANT_SELECT"] }, after: { ...GRANTS[0], privileges: ["SELECT"] } }],
        },
      ],
    },
  });
  await page.goto("/permissions");
  const row = page.getByRole("row", { name: /reporting/ });
  await expect(row).toContainText("Modified");
  await expect(row.getByTitle("removed")).toHaveText("− GRANT_SELECT");
});

test("adding a grant sends what it names and the privileges ticked", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "POST /api/v1/permissions": (route: Route) => {
      sent = route.request().postDataJSON();
      return { ...(sent as object), key: "x" };
    },
  });
  await page.goto("/permissions");
  await page.getByRole("button", { name: "+ Add grant" }).click();
  const d = page.getByRole("dialog");
  await d.getByLabel("Trino Identity").fill("bi_reader");
  await d.getByLabel("Catalog").fill("lake_iceberg");
  await d.getByRole("textbox", { name: /^Schema/ }).fill("curated");
  await d.getByLabel("INSERT").check();
  await d.getByRole("button", { name: "Stage" }).click();
  await expect.poll(() => sent).toEqual({ identity: "bi_reader", catalog: "lake_iceberg", schema: "curated", table: null, privileges: ["SELECT", "INSERT"] });
});

test("filters narrow the grants by identity", async ({ page }) => {
  await stubApi(page);
  await page.goto("/permissions");
  await page.getByLabel("Identity").selectOption("etl");
  await expect(page.getByText("1 of 3 grants")).toBeVisible();
});

test("selectors are reordered locally and staged as a whole list", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "PUT /api/v1/resource-groups/selectors": (route: Route) => {
      sent = route.request().postDataJSON();
      return sent;
    },
  });
  await page.goto("/resource-groups");
  await page.getByRole("button", { name: "Move selector 2 up" }).click();
  await expect(page.getByText("The order changed here and is not staged yet.")).toBeVisible();
  expect(sent).toBeNull();
  await page.getByRole("button", { name: "Stage the order" }).click();
  await expect.poll(() => sent).toEqual({ selectors: [SELECTORS[1], SELECTORS[0], SELECTORS[2], SELECTORS[3]] });
});

test("a CPU limit without a quota period is called out, because Trino will not start", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "PUT /api/v1/resource-groups/settings": (route: Route) => {
      sent = route.request().postDataJSON();
      return sent;
    },
  });
  await page.goto("/resource-groups");
  await expect(page.getByText("Needed now")).toBeVisible();
  await page.getByLabel("How often CPU limits reset").fill("1h");
  await page.getByRole("button", { name: "Stage", exact: true }).click();
  await expect.poll(() => sent).toEqual({ cpu_quota_period: "1h" });
});

test("a staged selector move is shown with where it was", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/review": {
      ...REVIEW_STAGED,
      sections: [
        ...REVIEW_STAGED.sections.filter((s) => s.section !== "resource_groups"),
        { section: "resource_groups", changes: [{ resource: "#selectors", change: "changed", before: [SELECTORS[0], SELECTORS[2], SELECTORS[3], SELECTORS[1]], after: SELECTORS }] },
      ],
    },
  });
  await page.goto("/resource-groups");
  await expect(page.getByRole("row", { name: /was 4 global\.etl\.batch/ })).toBeVisible();
  await page.goto("/review");
  await expect(page.getByRole("region", { name: "Resource groups selectors" })).toContainText("position 4");
});
