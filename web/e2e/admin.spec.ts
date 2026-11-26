import { test, expect, type Route } from "@playwright/test";
import { AUDIT, CLUSTER, stubApi } from "./fixtures";

/** The Admin's own surfaces: values outside Snapshots, raw files, enforcement, maintenance. */

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

test("an unknown path says so", async ({ page }) => {
  await stubApi(page);
  await page.goto("/no-such-screen");
  await expect(page.getByRole("heading", { name: "Not found" })).toBeVisible();
});

// --- Server configuration ------------------------------------------------------------------

test("server configuration shows each role's properties, masking what looks like a credential", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");

  const config = page.getByRole("region", { name: "config.properties" });
  await expect(config.getByRole("row", { name: /query.max-execution-time/ })).toContainText("2h");
  const secret = config.getByRole("row", { name: /internal-communication.shared-secret/ });
  await expect(secret).toContainText("••••••••");
  await expect(secret).not.toContainText("s3cret-shared");
  await secret.getByRole("button", { name: "Show" }).click();
  await expect(secret).toContainText("s3cret-shared");
  await expect(page.getByText("ends every running query")).toBeVisible();

  await page.getByRole("tab", { name: "Workers" }).click();
  await expect(config.getByRole("row", { name: /task.concurrency/ })).toContainText("16");
  await expect(page.getByText("no query is lost")).toBeVisible();
});

test("saving sends both roles whole, and a refusal says why in Apchi's words", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    ...ADMIN_STUBS,
    "PUT /api/v1/admin/server-configuration": (route: Route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          code: "unprocessable_payload",
          message: "config.properties: discovery.uri is wiring and cannot be set here; it comes from the coordinator Service",
          request_id: "r",
        }),
      });
    },
  });
  await page.goto("/admin/server-configuration");
  await expect(page.getByRole("button", { name: "Save both roles" })).toBeDisabled();

  await page.getByRole("tab", { name: "Workers" }).click();
  await page.getByLabel("log.properties property").fill("io.trino.execution");
  await page.getByLabel("log.properties value").fill("DEBUG");
  await page.getByRole("region", { name: "log.properties" }).getByRole("button", { name: "Add" }).click();
  await page.getByRole("button", { name: "Save both roles" }).click();

  await expect.poll(() => sent).toEqual({
    coordinator: {
      "config.properties": { "query.max-execution-time": "2h", "internal-communication.shared-secret": "s3cret-shared" },
      "node.properties": {},
      "log.properties": {},
      "event-listener.properties": {},
    },
    worker: {
      "config.properties": { "task.concurrency": "16" },
      "node.properties": {},
      "log.properties": { "io.trino.execution": "DEBUG" },
    },
  });
  await expect(page.getByText("discovery.uri is wiring and cannot be set here")).toBeVisible();
});

test("a credential-looking value is typed into a password field", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");
  await page.getByLabel("config.properties property").fill("http-server.authentication.oauth2.client-secret");

  await expect(page.getByLabel("config.properties value")).toHaveAttribute("type", "password");
});

test("removing a property and discarding puts it back", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");
  const config = page.getByRole("region", { name: "config.properties" });
  await config.getByRole("button", { name: "Remove query.max-execution-time" }).click();
  await expect(config.getByRole("row", { name: /query.max-execution-time/ })).toHaveCount(0);

  await page.getByRole("button", { name: "Discard changes" }).click();

  await expect(config.getByRole("row", { name: /query.max-execution-time/ })).toContainText("2h");
  await expect(page.getByRole("button", { name: "Save both roles" })).toBeDisabled();
});

test("the rendered files show the wiring above the Admin's lines, credentials masked", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");
  const rendered = page.getByRole("region", { name: "Rendered files" });

  await expect(rendered).toContainText("coordinator=true");
  await expect(rendered).toContainText("internal-communication.shared-secret=••••••••");
  await expect(rendered).not.toContainText("s3cret-shared");
  await rendered.getByRole("button", { name: "Show values" }).click();
  await expect(rendered).toContainText("s3cret-shared");

  await page.getByRole("tab", { name: "Workers" }).click();
  await expect(rendered).toContainText("shutdown.grace-period=120s");
});

test("admin values counts the properties set on each role and links to them", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin");

  await expect(page.getByText("coordinator: 2 · workers: 1")).toBeVisible();
  await page.locator('a[href="/admin/server-configuration"]').filter({ hasText: "Manage" }).click();
  await expect(page).toHaveURL(/\/admin\/server-configuration$/);
});

test("an event listener is the Admin's: a plugin, the properties it requires, and the pointer Apchi writes", async ({ page }) => {
  type Sent = { coordinator: Record<string, unknown>; worker: Record<string, unknown> };
  let sent = null as Sent | null;
  await stubApi(page, {
    ...ADMIN_STUBS,
    "PUT /api/v1/admin/server-configuration": (route: Route) => {
      sent = route.request().postDataJSON();
      return sent;
    },
  });
  await page.goto("/admin/server-configuration");
  const card = page.getByRole("region", { name: "Event listener" });
  await expect(card).toContainText("None is configured");
  await expect(card).toContainText("Restarts the coordinator");

  await card.getByLabel("Event listener plugin").selectOption("http");
  await expect(card.getByRole("status")).toContainText("http-event-listener.connect-ingest-uri");
  await card.getByRole("button", { name: "http-event-listener.connect-ingest-uri" }).click();
  await card.getByLabel("event-listener.properties value").fill("http://collector:8080/events");
  await card.getByRole("button", { name: "Add" }).click();
  await expect(card.getByRole("status")).toHaveCount(0);
  await page.getByRole("button", { name: "Save both roles" }).click();

  await expect.poll(() => sent?.coordinator["event-listener.properties"]).toEqual({
    "event-listener.name": "http",
    "http-event-listener.connect-ingest-uri": "http://collector:8080/events",
  });
  expect(sent?.worker).not.toHaveProperty("event-listener.properties");
});

test("a workers' tab has no event listener: a worker sends no query events", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");
  await page.getByRole("tab", { name: "Workers" }).click();

  await expect(page.getByRole("region", { name: "Event listener" })).toHaveCount(0);
});

test("a plugin Apchi has no schema for is named by hand and left to Trino", async ({ page }) => {
  await stubApi(page, ADMIN_STUBS);
  await page.goto("/admin/server-configuration");
  const card = page.getByRole("region", { name: "Event listener" });

  await card.getByLabel("Event listener plugin").selectOption("__other");
  await card.getByLabel("Event listener plugin name").fill("openlineage");

  await expect(card).toContainText("checked by Trino at the next Apply");
  await card.getByRole("button", { name: "Remove the listener" }).click();
  await expect(card).toContainText("None is configured");
});
