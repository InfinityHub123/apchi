import { test, expect, type Route } from "@playwright/test";
import { AUDIT, CLUSTER, Reply, stubApi } from "./fixtures";

/** The Admin's own surfaces: values outside Snapshots, raw files, enforcement, maintenance, adoption. */

const ADMIN_STUBS = {
  "GET /api/v1/admin/permissions/enforcement": { enforced: false },
  "GET /api/v1/admin/files": [
    { path: "/etc/trino/jvm.config", bytes: 1229 },
    { path: "/etc/trino/log.properties", bytes: 214 },
  ],
};

test("Admin Apply starts the Admin pipeline and follows it", async ({ page }) => {
  await stubApi(page, {
    ...ADMIN_STUBS,
    "POST /api/v1/admin/applies": { id: "apl_admin", kind: "admin_apply", stage: "validating", history: [], started_at: "2026-11-17T15:00:00Z" },
    "GET /api/v1/applies/*": { id: "apl_admin", kind: "admin_apply", stage: "validating", history: [{ stage: "validating", at: "2026-11-17T15:00:00Z" }], started_at: "2026-11-17T15:00:00Z", rolled_out: false },
    "GET /api/v1/applies/apl_admin/events": (route: Route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }),
  });
  await page.goto("/admin");
  await expect(page.getByText("jvm.config · log.properties")).toBeVisible();
  await page.getByRole("button", { name: "Admin Apply…" }).click();
  await expect(page).toHaveURL(/\/apply\?id=apl_admin/);
  await expect(page.getByText("an Admin Apply creates no Snapshot")).toBeVisible();
});

test("raw files list paths and sizes, never content, and are written by path", async ({ page }) => {
  let wrote: { url: string; body: unknown } | null = null;
  await stubApi(page, {
    ...ADMIN_STUBS,
    "GET /api/v1/audit": { items: [{ ...AUDIT[0], category: "admin", resource: "/etc/trino/jvm.config", actor: "dana" }], next: null },
  });
  await page.route("**/api/v1/admin/files/**", (route) => {
    wrote = { url: new URL(route.request().url()).pathname, body: route.request().postDataJSON() };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ path: "/etc/trino/extra.properties", bytes: 20 }) });
  });
  await page.goto("/admin/raw-files");
  await expect(page.getByRole("row", { name: /jvm.config/ })).toContainText("1.2 KB");
  await expect(page.getByRole("row", { name: /jvm.config/ })).toContainText("by dana");
  await page.getByLabel(/^Path/).fill("/etc/trino/extra.properties");
  await page.getByLabel(/^Content/).fill("query.max-memory=50GB\n");
  await page.getByRole("button", { name: "Write file" }).click();
  await expect.poll(() => wrote).toEqual({ url: "/api/v1/admin/files/etc/trino/extra.properties", body: { content: "query.max-memory=50GB\n" } });
});

test("a refused raw file says why, in Apchi's words", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.route("**/api/v1/admin/files/**", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ code: "unprocessable_payload", message: "This deployment mounts /etc/trino as one directory.", request_id: "r" }),
    }),
  );
  await page.goto("/admin/raw-files");
  await page.getByLabel(/^Content/).fill("x");
  await page.getByLabel(/^Path/).fill("/etc/trino/x.properties");
  await page.getByRole("button", { name: "Write file" }).click();
  await expect(page.getByText("This deployment mounts /etc/trino as one directory.")).toBeVisible();
});

test("turning enforcement on warns who loses access, then sends it", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    ...ADMIN_STUBS,
    "PUT /api/v1/admin/permissions/enforcement": (route: Route) => {
      sent = route.request().postDataJSON();
      return { enforced: true };
    },
  });
  await page.goto("/admin/enforcement");
  await expect(page.getByText("every identity without a grant loses everything it had")).toBeVisible();
  await page.getByRole("button", { name: "Turn enforcement on" }).click();
  await expect.poll(() => sent).toEqual({ enforced: true });
});

test("Maintenance Mode shows its history from the audit and engages with a reason", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "GET /api/v1/audit": {
      items: [
        { ...AUDIT[0], id: "m2", category: "admin", resource: "maintenance_mode", action: "released", actor: "dana", detail: "node pool fixed" },
        { ...AUDIT[0], id: "m1", category: "admin", resource: "maintenance_mode", action: "engaged", actor: "apchi", detail: "engaged by auto_rollback_failure: Auto Rollback failed" },
      ],
      next: null,
    },
    "PUT /api/v1/admin/maintenance-mode": (route: Route) => {
      sent = route.request().postDataJSON();
      return { ...CLUSTER.maintenance, engaged: true, reason: "upgrade" };
    },
  });
  await page.goto("/maintenance");
  await expect(page.getByRole("row", { name: /released/ })).toContainText("node pool fixed");
  await expect(page.getByRole("row", { name: /engaged/ })).toContainText("apchi, automatically");
  await expect(page.getByRole("row", { name: /engaged/ })).toContainText("Auto Rollback failed");
  await page.getByLabel(/^Reason/).fill("Trino upgrade");
  await page.getByRole("button", { name: "Engage Maintenance Mode" }).click();
  await expect.poll(() => sent).toEqual({ engaged: true, reason: "Trino upgrade" });
});

test("adoption shows what it read, what is outstanding, and lets an incomplete catalog be supplied", async ({ page }) => {
  let supplied: unknown = null;
  await stubApi(page, {
    "GET /api/v1/admin/adoption/discovery": {
      read_at: "2026-11-17T16:10:00Z",
      complete: false,
      outstanding: [{ kind: "incomplete", section: "catalogs", path: "warehouse_hive", detail: "Trino is serving 'warehouse_hive' using the 'hive' connector." }],
      changed_since_last_read: [],
      sections: [
        { section: "catalogs", readable: true, looked_at: ["/etc/trino/catalog/"], guessed_because: null, resources: { a: {}, b: {} }, amended: [], problems: [] },
        { section: "permissions", readable: true, looked_at: ["rules.json"], guessed_because: null, resources: { g: {} }, amended: [], problems: [] },
      ],
      admin_values: { preserved_access_control: { tables: [1, 2] } },
    },
    "PUT /api/v1/admin/adoption/discovery/catalogs/*": (route: Route) => {
      supplied = route.request().postDataJSON();
      return {};
    },
  });
  await page.goto("/admin/adoption");
  await expect(page.getByText("Not complete · 1 outstanding")).toBeVisible();
  await expect(page.getByRole("button", { name: "Adopt and apply" })).toBeDisabled();
  await page.getByRole("button", { name: "Supply properties" }).click();
  const d = page.getByRole("dialog");
  await d.getByLabel("Connector").fill("hive");
  await d.getByLabel(/^Properties/).fill("hive.metastore.uri=thrift://metastore:9083");
  await d.getByRole("button", { name: "Save to discovery" }).click();
  await expect.poll(() => supplied).toEqual({ name: "warehouse_hive", connector: "hive", properties: { "hive.metastore.uri": "thrift://metastore:9083" } });
});

test("with no discovery yet, adoption says nothing has been read", async ({ page }) => {
  await stubApi(page, { "GET /api/v1/admin/adoption/discovery": new Reply(404, { code: "not_found", message: "No discovery", request_id: "r" }) });
  await page.goto("/admin/adoption");
  await expect(page.getByText(/Nothing has been read yet/)).toBeVisible();
});

test("an unknown path says so", async ({ page }) => {
  await stubApi(page);
  await page.goto("/no-such-screen");
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});
