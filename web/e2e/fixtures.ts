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

export const SNAPSHOTS = [
  { number: 118, created_at: "2026-08-18T09:42:00Z", apply_id: "apl_8f31", sections: {} },
  { number: 117, created_at: "2026-08-14T11:05:00Z", apply_id: "apl_7c02", sections: {} },
];

/** Newest first, as the endpoint answers. */
export const AUDIT = [
  {
    id: "ae_4",
    at: "2026-08-19T10:21:00Z",
    actor: "dana",
    category: "catalogs",
    resource: "finance_pg",
    action: "created",
    outcome: "succeeded",
    via: "password",
  },
  {
    id: "ae_3",
    at: "2026-08-19T10:19:00Z",
    actor: "sam",
    category: "certificate_mapping",
    resource: "CN=*.analytics.corp",
    action: "updated",
    outcome: "succeeded",
    via: "password",
  },
  {
    id: "ae_2",
    at: "2026-08-19T10:18:00Z",
    actor: "rui",
    category: "catalogs",
    resource: "events_kafka",
    action: "updated",
    outcome: "refused",
    via: "password",
  },
];

export async function stubApi(
  page: Page,
  opts: {
    review?: unknown;
    health?: unknown;
    caller?: unknown;
    snapshots?: unknown;
    audit?: unknown;
    certificates?: unknown;
  } = {},
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
  await page.route("**/api/v1/auth/sign-in", (r) => r.fulfill(json({ mode: "password" })));
  await page.route("**/api/v1/auth/logout", (r) => r.fulfill({ status: 204, body: "" }));
  await page.route("**/api/v1/snapshots*", (r) =>
    r.fulfill(json(opts.snapshots ?? { items: SNAPSHOTS, next: null })),
  );
  await page.route("**/api/v1/audit*", (r) =>
    r.fulfill(json(opts.audit ?? { items: AUDIT, next: null })),
  );
  await page.route("**/api/v1/certificates*", (r) =>
    r.fulfill(json(opts.certificates ?? [])),
  );
}
