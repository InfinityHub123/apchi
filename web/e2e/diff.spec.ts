import { test, expect } from "@playwright/test";
import { stubApi } from "./fixtures";

/**
 * §21 requires the full Candidate diff before an Apply. The table is the inventory; this is
 * the other half, because "events_kafka changed" does not tell an Operator whether a broker
 * moved or the whole topic list was replaced.
 */

const WITH_VALUES = {
  has_changes: true,
  base_snapshot: 118,
  sections: [
    {
      section: "catalogs",
      changes: [
        {
          resource: "events_kafka",
          change: "changed",
          before: {
            connector: "kafka",
            properties: { "kafka.nodes": "old.internal:9092", "kafka.table-names": "events" },
          },
          after: {
            connector: "kafka",
            properties: { "kafka.nodes": "new.internal:9092", "kafka.table-names": "events" },
          },
        },
        {
          resource: "secretful_pg",
          change: "added",
          before: null,
          after: {
            connector: "postgresql",
            properties: {
              "connection-url": "jdbc:postgresql://db.internal:5432/x",
              "connection-password": "hunter2-the-real-one",
            },
          },
        },
      ],
    },
  ],
  cost: { restarts_coordinator: false, queries_at_risk: null, warning: null },
};

test("a change opens to show which field moved, and which did not", async ({ page }) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  await page.getByRole("button", { name: /events_kafka/ }).click();

  const diff = page.getByRole("table", { name: /adds, alters or removes/i });
  await expect(diff).toContainText("properties.kafka.nodes");
  await expect(diff).toContainText("old.internal:9092");
  await expect(diff).toContainText("new.internal:9092");
  // The topic list is identical on both sides, so showing it would be noise in a diff an
  // Operator is scanning for what actually changed.
  await expect(diff).not.toContainText("kafka.table-names");
});

test("rows are collapsed until asked, and say so to a screen reader", async ({ page }) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  const row = page.getByRole("button", { name: /events_kafka/ });
  await expect(row).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("old.internal:9092")).toHaveCount(0);

  await row.click();
  await expect(row).toHaveAttribute("aria-expanded", "true");
});

test("a secret value is not on screen until somebody asks for it", async ({ page }) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  await page.getByRole("button", { name: /secretful_pg/ }).click();

  // The API returns it -- §12 says these users are trusted -- but a diff read during a
  // screen-share should not put a credential on screen by accident.
  await expect(page.getByText("hunter2-the-real-one")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Reveal properties.connection-password/ })).toBeVisible();

  await page.getByRole("button", { name: /Reveal properties.connection-password/ }).click();
  await expect(page.getByText("hunter2-the-real-one")).toBeVisible();
});

test("a non-secret value beside a secret one is shown normally", async ({ page }) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  await page.getByRole("button", { name: /secretful_pg/ }).click();

  // Masking everything in a resource that happens to hold a credential would make the diff
  // useless; only the field whose name says it carries one is hidden.
  await expect(page.getByText("jdbc:postgresql://db.internal:5432/x")).toBeVisible();
});

test("an added resource shows every field as arriving, with nothing before it", async ({
  page,
}) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  await page.getByRole("button", { name: /secretful_pg/ }).click();

  const row = diffRow(page, "properties.connection-url");
  await expect(row).toContainText("not set");
});

test("a removed resource shows what is going away", async ({ page }) => {
  await stubApi(page, {
    review: {
      ...WITH_VALUES,
      sections: [
        {
          section: "catalogs",
          changes: [
            {
              resource: "legacy_mysql",
              change: "removed",
              before: { connector: "mysql", properties: { "connection-url": "jdbc:mysql://old" } },
              after: null,
            },
          ],
        },
      ],
    },
  });
  await page.goto("/");

  await page.getByRole("button", { name: /legacy_mysql/ }).click();

  const row = diffRow(page, "properties.connection-url");
  await expect(row).toContainText("jdbc:mysql://old");
  await expect(row).toContainText("not set");
});

test("the diff is reachable and operable from the keyboard", async ({ page }) => {
  await stubApi(page, { review: WITH_VALUES });
  await page.goto("/");

  const row = page.getByRole("button", { name: /events_kafka/ });
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(row).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("new.internal:9092")).toBeVisible();
});

/** A row of the field diff itself, not the table row that contains the whole diff. */
function diffRow(page: import("@playwright/test").Page, field: string) {
  return page
    .getByRole("table", { name: /adds, alters or removes/i })
    .getByRole("row")
    .filter({ hasText: field });
}
