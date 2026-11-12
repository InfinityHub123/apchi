import type { Page } from "@playwright/test";

/** A Review with one no-restart Section and one that forces a rollout. */
export const REVIEW_STAGED = {
  has_changes: true,
  base_snapshot: 118,
  sections: [
    {
      section: "catalogs",
      changes: [
        { resource: "finance_pg", change: "added" },
        { resource: "events_kafka", change: "changed" },
        { resource: "legacy_mysql", change: "removed" },
      ],
    },
    {
      section: "certificate_mapping",
      changes: [{ resource: "CN=*.analytics.corp", change: "changed" }],
    },
  ],
  cost: {
    restarts_coordinator: true,
    queries_at_risk: 47,
    warning: "Applying this will terminate running queries.",
  },
};

export const REVIEW_CLEAN = {
  has_changes: false,
  base_snapshot: 118,
  sections: [],
  cost: { restarts_coordinator: false, queries_at_risk: null, warning: null },
};

export async function stubApi(
  page: Page,
  opts: { review?: unknown; health?: unknown; caller?: unknown } = {},
) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/health", (r) =>
    r.fulfill(json(opts.health ?? { status: "ok", mongo: "ok" })),
  );
  await page.route("**/api/v1/review", (r) => r.fulfill(json(opts.review ?? REVIEW_STAGED)));
  await page.route("**/api/v1/auth/me", (r) =>
    opts.caller === null
      ? r.fulfill({ status: 401, contentType: "application/json", body: "{}" })
      : r.fulfill(json(opts.caller ?? { subject: "dana", role: "operator", via: "password" })),
  );
  await page.route("**/api/v1/snapshots", (r) => r.fulfill(json([])));
}
