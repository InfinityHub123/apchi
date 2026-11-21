import { test, expect, type Route } from "@playwright/test";
import { Reply, stubApi } from "./fixtures";

/** Catalogs: the listing, one Catalog's detail, and the schema-driven add and edit forms. */

test("the listing marks staged rows, pass-through connectors and an expiring certificate", async ({ page }) => {
  await stubApi(page);
  await page.goto("/catalogs");
  const finance = page.getByRole("row", { name: /finance_pg/ });
  await expect(finance).toContainText("Modified · staged");
  await expect(finance).toContainText("Checked by Apchi");
  await expect(page.getByRole("row", { name: /marketing_mysql/ })).toContainText("Pass-through");
  // Removed from the Candidate, so absent from the listing -- but still shown, because it is
  // a staged change somebody may need to undo.
  await expect(page.getByRole("row", { name: /legacy_oracle/ })).toContainText("Removed · staged");
  await expect(page.getByRole("row", { name: /lake_iceberg/ })).toContainText("In Snapshot 13");
});

test("a Catalog's detail diffs what moved and masks the password", async ({ page }) => {
  await stubApi(page);
  await page.goto("/catalogs/finance_pg");
  const detail = page.getByRole("region", { name: "finance_pg" });
  await expect(detail).toContainText("staged by jo");
  await expect(detail).toContainText("pg-fin-01");
  await expect(detail).toContainText("pg-fin-02");
  await expect(detail).not.toContainText("hunter2");
});

test("Delete asks first and says the catalog is dropped at the next Apply", async ({ page }) => {
  let deleted = false;
  await stubApi(page, {
    "DELETE /api/v1/catalogs/*": () => {
      deleted = true;
      return null;
    },
  });
  await page.goto("/catalogs/lake_iceberg");
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("dialog")).toContainText("dropped from the running cluster");
  expect(deleted).toBe(false);
  await page.getByRole("button", { name: "Stage the removal" }).click();
  await expect.poll(() => deleted).toBe(true);
});

test("adding an Iceberg catalog follows its branch: the catalog type decides what is required", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "POST /api/v1/catalogs": (route: Route) => {
      sent = route.request().postDataJSON();
      return { name: "lake", connector: "iceberg", supported: true, certificate: null, properties: {} };
    },
  });
  await page.goto("/catalogs/new");
  await page.getByRole("button", { name: /Iceberg/ }).click();
  await page.getByLabel("Name", { exact: false }).first().fill("lake");
  // The default branch requires the metastore URI; choosing REST swaps it for the REST URI.
  await expect(page.getByText("hive.metastore.uri")).toBeVisible();
  await page.getByRole("radio", { name: "rest" }).click();
  await expect(page.getByText("hive.metastore.uri")).toHaveCount(0);
  await page.getByRole("textbox", { name: "iceberg.rest-catalog.uri", exact: true }).fill("https://rest.internal");
  await page.getByRole("button", { name: "+ iceberg.file-format" }).click();
  await page.getByRole("textbox", { name: "iceberg.file-format", exact: true }).fill("PARQUET");
  await page.getByRole("button", { name: "Stage catalog" }).click();
  await expect.poll(() => sent).toEqual({
    name: "lake",
    connector: "iceberg",
    properties: {
      "iceberg.catalog.type": "rest",
      "iceberg.rest-catalog.uri": "https://rest.internal",
      "iceberg.file-format": "PARQUET",
    },
    certificate: null,
  });
  await expect(page.getByText("not on the cluster yet")).toBeVisible();
});

test("a refusal marks the property it names, in Apchi's words", async ({ page }) => {
  await stubApi(page, {
    "POST /api/v1/catalogs": new Reply(422, {
      code: "unprocessable_payload",
      message: "The properties are not valid for the 'postgresql' connector.",
      details: [{ property: "connection-url", problem: "'connection-url' must be a JDBC URL" }],
      request_id: "r",
    }),
  });
  await page.goto("/catalogs/new");
  await page.getByRole("button", { name: /PostgreSQL/ }).click();
  await page.getByLabel("Name", { exact: false }).first().fill("pg");
  await page.getByRole("textbox", { name: "connection-url", exact: true }).fill("nope");
  await page.getByRole("button", { name: "Stage catalog" }).click();
  await expect(page.getByText("The properties are not valid for the 'postgresql' connector.")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "connection-url", exact: true })).toHaveAttribute("aria-invalid", "true");
});

test("a password field is masked as it is typed, and masked in the request preview", async ({ page }) => {
  await stubApi(page);
  await page.goto("/catalogs/new");
  await page.getByRole("button", { name: /PostgreSQL/ }).click();
  await page.getByRole("button", { name: "+ connection-password" }).click();
  const field = page.getByRole("textbox", { name: "connection-password", exact: true });
  await expect(field).toHaveAttribute("type", "password");
  await field.fill("s3cret");
  await expect(page.locator("pre.code")).not.toContainText("s3cret");
});

test("a custom connector is pass-through, with free properties", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "POST /api/v1/catalogs": (route: Route) => {
      sent = route.request().postDataJSON();
      return {};
    },
  });
  await page.goto("/catalogs/new");
  await page.getByRole("button", { name: /Custom/ }).click();
  await page.getByLabel("Name", { exact: false }).first().fill("ch");
  await page.getByLabel("Connector name").fill("clickhouse");
  await page.getByLabel("New property name").fill("connection-url");
  await page.getByRole("button", { name: "+ Add property" }).click();
  await page.getByRole("textbox", { name: "connection-url", exact: true }).fill("jdbc:clickhouse://ch:8123");
  await page.getByRole("button", { name: "Stage catalog" }).click();
  await expect.poll(() => sent).toEqual({
    name: "ch",
    connector: "clickhouse",
    properties: { "connection-url": "jdbc:clickhouse://ch:8123" },
    certificate: null,
  });
});

test("editing replaces the whole property map, as PATCH documents", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "PATCH /api/v1/catalogs/*": (route: Route) => {
      sent = route.request().postDataJSON();
      return {};
    },
  });
  await page.goto("/catalogs/finance_pg/edit");
  await page.getByRole("textbox", { name: "connection-url", exact: true }).fill("jdbc:postgresql://pg-fin-03:5432/finance");
  await page.getByRole("button", { name: "Stage changes" }).click();
  await expect.poll(() => sent).toEqual({
    properties: { "connection-url": "jdbc:postgresql://pg-fin-03:5432/finance", "connection-password": "hunter2" },
    certificate: "finance_pg_tls",
  });
});

test("Section Revert previews with a dry run before staging anything", async ({ page }) => {
  const calls: unknown[] = [];
  const effect = (staged: boolean) => ({
    sections: ["catalogs"],
    from_snapshot: 10,
    other_sections_stay_at: 13,
    catalogs_dropped: ["clickstream_kafka"],
    catalogs_created: ["legacy_oracle"],
    catalogs_replaced: ["finance_pg"],
    cost: { restarts_coordinator: false, queries_at_risk: null, rollout_sections: [], warning: null },
    staged,
    summary: "Catalogs will be restored to Snapshot 10.",
  });
  await stubApi(page, {
    "POST /api/v1/catalogs/revert": (route: Route) => {
      const body = route.request().postDataJSON() as { dry_run: boolean };
      calls.push(body);
      return effect(!body.dry_run);
    },
  });
  await page.goto("/catalogs");
  await page.getByRole("button", { name: "Revert Section…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Snapshot").selectOption("10");
  await dialog.getByRole("button", { name: "Preview" }).click();
  await expect(dialog).toContainText("This combination has never existed");
  await expect(dialog).toContainText("clickstream_kafka");
  expect(calls).toEqual([{ snapshot: 10, dry_run: true }]);
  await dialog.getByRole("button", { name: "Stage revert" }).click();
  await expect.poll(() => calls.length).toBe(2);
  expect(calls[1]).toEqual({ snapshot: 10, dry_run: false });
});
