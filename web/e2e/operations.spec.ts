import { test, expect, type Route } from "@playwright/test";
import { AUDIT, Reply, SNAPSHOTS, stubApi } from "./fixtures";

/** Snapshots, the audit trail, import/export and API tokens. */

const effect = (staged: boolean, over: Record<string, unknown> = {}) => ({
  sections: ["catalogs"],
  from_snapshot: 12,
  other_sections_stay_at: 13,
  catalogs_dropped: [],
  catalogs_created: ["legacy_oracle"],
  catalogs_replaced: [],
  cost: { restarts_coordinator: false, queries_at_risk: null, rollout_sections: [], warning: null },
  staged,
  summary: "Catalogs will be restored to Snapshot 12.",
  ...over,
});

test("a Snapshot previews its Section Revert from Apchi's dry run and stages on request", async ({ page }) => {
  const calls: unknown[] = [];
  await stubApi(page, {
    "GET /api/v1/snapshots/*": { ...SNAPSHOTS[1], sections: { catalogs: { a: {}, b: {} }, permissions: {} } },
    "GET /api/v1/applies": { items: [], next: null },
    "POST /api/v1/catalogs/revert": (route: Route) => {
      const body = route.request().postDataJSON() as { dry_run: boolean };
      calls.push(body);
      return effect(!body.dry_run);
    },
  });
  await page.goto("/snapshots?n=12");
  await expect(page.getByText("Catalogs will be restored to Snapshot 12.")).toBeVisible();
  await expect(page.getByText(/This combination has never existed/)).toBeVisible();
  expect(calls).toEqual([{ snapshot: 12, dry_run: true }]);
  await page.getByRole("button", { name: "Stage revert" }).click();
  await expect(page.getByText(/Staged\./)).toBeVisible();
  expect(calls[1]).toEqual({ snapshot: 12, dry_run: false });
});

test("Full Rollback warns that everyone's staged work goes, and previews before staging", async ({ page }) => {
  const calls: unknown[] = [];
  await stubApi(page, {
    "GET /api/v1/snapshots/*": { ...SNAPSHOTS[1], sections: {} },
    "GET /api/v1/applies": { items: [], next: null },
    "POST /api/v1/catalogs/revert": effect(false),
    "POST /api/v1/candidate/rollback": (route: Route) => {
      const body = route.request().postDataJSON() as { dry_run: boolean };
      calls.push(body);
      return effect(!body.dry_run, { other_sections_stay_at: null, summary: "Every Section restored from Snapshot 12." });
    },
  });
  await page.goto("/snapshots?n=12");
  await page.getByRole("button", { name: "Full Rollback…" }).click();
  const d = page.getByRole("dialog");
  await expect(d).toContainText("including other people's");
  await expect(d).toContainText("Every Section restored from Snapshot 12.");
  await d.getByRole("button", { name: "Stage the Full Rollback" }).click();
  await expect.poll(() => calls).toEqual([{ snapshot: 12, dry_run: true }, { snapshot: 12, dry_run: false }]);
});

test("the live Snapshot offers no revert to itself", async ({ page }) => {
  await stubApi(page, { "GET /api/v1/snapshots/*": { ...SNAPSHOTS[0], sections: {} }, "GET /api/v1/applies": { items: [], next: null } });
  await page.goto("/snapshots?n=13");
  await expect(page.getByRole("button", { name: "Full Rollback…" })).toHaveCount(0);
  await expect(page.getByText("Section Revert")).toHaveCount(0);
});

test("the audit names a token by the name it had, and groups an Apply's events", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/audit": {
      items: [
        { ...AUDIT[0], id: "t1", actor: "jo", via: "token", token_id: "tok_1", token_name: "ci-pipeline", resource: "analyst_ro", category: "permissions" },
        { ...AUDIT[0], id: "p1", actor: "mira", category: "pipeline", action: "applied", resource: null, apply_id: "apl_7f3c", snapshot: 13 },
        { ...AUDIT[0], id: "p2", actor: "mira", category: "pipeline", action: "attempted", resource: null, apply_id: "apl_7f3c" },
      ],
      next: null,
    },
  });
  await page.goto("/audit");
  await expect(page.getByRole("row", { name: /analyst_ro/ })).toContainText("token ci-pipeline");
  await expect(page.getByText("Apply apl_7f3c · 2 entries")).toBeVisible();
});

test("audit filters are sent to the API and carried into the export link", async ({ page }) => {
  const asked: string[] = [];
  await stubApi(page, {
    "GET /api/v1/audit": (_r: Route, url: URL) => {
      asked.push(url.search);
      return { items: [], next: null };
    },
  });
  await page.goto("/audit");
  await page.getByLabel("Outcome").selectOption("refused");
  await page.getByLabel("Actor").fill("mira");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect.poll(() => asked.at(-1)).toContain("outcome=refused");
  expect(asked.at(-1)).toContain("actor=mira");
  await expect(page.getByRole("link", { name: /Export what's filtered/ })).toHaveAttribute("href", /outcome=refused/);
});

test("a minted token's secret is shown once, until dismissed", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "GET /api/v1/tokens": { items: [], next: null },
    "POST /api/v1/tokens": (route: Route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          secret: "apchi_tk_9f2c71e04b8d",
          token: { id: "tok_1", name: "nightly-export", owner: "dana", created_at: "2026-11-17T10:00:00Z", expires_at: null, last_used_at: null, revoked_at: null },
        }),
      });
    },
  });
  await page.goto("/tokens");
  await page.getByLabel("Name").fill("nightly-export");
  await page.getByLabel(/^Never/).check();
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByRole("region", { name: "New token secret" })).toContainText("apchi_tk_9f2c71e04b8d");
  expect(sent).toEqual({ name: "nightly-export" });
  await page.getByRole("button", { name: "I have saved it" }).click();
  await expect(page.getByText("apchi_tk_9f2c71e04b8d")).toHaveCount(0);
});

test("an import names the Sections it would empty, and is refused whole with every problem", async ({ page }) => {
  await stubApi(page, {
    "PUT /api/v1/candidate": new Reply(422, {
      code: "unprocessable_payload",
      message: "The document was not imported; nothing was changed.",
      details: [{ resource: "sales_pg", problem: "connection-url is required" }],
      request_id: "r",
    }),
  });
  await page.goto("/import-export");
  await page.getByLabel("Exported document").setInputFiles({
    name: "export.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify({ format: 1, sections: { catalogs: {} }, admin: null })),
  });
  await expect(page.getByText(/Missing, so they would be emptied: Client certificates/)).toBeVisible();
  const go = page.getByRole("button", { name: "Import" });
  await expect(go).toBeDisabled();
  await page.getByLabel("I understand this replaces everything staged").check();
  await go.click();
  await expect(page.getByText("The document was not imported; nothing was changed.")).toBeVisible();
  await expect(page.getByText("sales_pg: connection-url is required")).toBeVisible();
});
