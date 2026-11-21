import type { Page, Route } from "@playwright/test";

/**
 * A stubbed Apchi, shaped from the committed contract.
 *
 * The UI is tested against the built bundle with every `/api/v1` answer supplied here,
 * because these tests are about what the UI does with an answer; producing a real one takes
 * a MongoDB and a Trino, which the Python tiers already have.
 *
 * One dispatcher rather than a `page.route` per test, so a request nobody stubbed answers a
 * loud 404 naming itself instead of hanging until a timeout says nothing.
 */

export const NOW = "2026-11-17T15:41:00Z";
export const SNAPSHOT_AT = "2026-11-11T14:02:00Z";

export const CLUSTER = {
  snapshot: 13,
  snapshot_at: SNAPSHOT_AT,
  maintenance: { engaged: false, engaged_at: null, engaged_by: null, reason: null },
  permissions_enforced: false,
  last_apply: {
    id: "a-7f3c",
    stage: "succeeded",
    started_at: "2026-11-11T13:58:00Z",
    finished_at: SNAPSHOT_AT,
    verified: true,
    snapshot: 13,
  },
};

export const PROBE = { at: NOW, coordinator: "up", workers: 8 };

export const REVIEW_STAGED = {
  base_snapshot: 13,
  has_changes: true,
  sections: [
    {
      section: "catalogs",
      changes: [
        {
          resource: "finance_pg",
          change: "changed",
          before: { connector: "postgresql", properties: { "connection-url": "jdbc:postgresql://pg-fin-01:5432/finance" } },
          after: { connector: "postgresql", properties: { "connection-url": "jdbc:postgresql://pg-fin-02:5432/finance" } },
        },
        { resource: "marketing_mysql", change: "added", after: { connector: "mysql", properties: {} } },
        { resource: "legacy_oracle", change: "removed", before: { connector: "oracle", properties: {} } },
      ],
    },
    { section: "client_certificates", changes: [] },
    { section: "certificate_mapping", changes: [] },
    { section: "permissions", changes: [] },
    {
      section: "resource_groups",
      changes: [{ resource: "global.etl.batch", change: "changed", before: { hardCpuLimit: null }, after: { hardCpuLimit: "4h" } }],
    },
    {
      section: "event_listeners",
      changes: [{ resource: "events_kafka", change: "changed", before: { type: "kafka", properties: { a: "1" } }, after: { type: "kafka", properties: { a: "2" } } }],
    },
  ],
  cost: {
    restarts_coordinator: true,
    queries_at_risk: 14,
    rollout_sections: ["certificate_mapping", "resource_groups", "event_listeners"],
    warning: "Applying this will terminate running queries.",
  },
  size_bytes: 421_888,
  limit_bytes: 15 * 1024 * 1024,
};

export const REVIEW_CLEAN = {
  ...REVIEW_STAGED,
  has_changes: false,
  sections: REVIEW_STAGED.sections.map((s) => ({ ...s, changes: [] })),
  cost: { restarts_coordinator: false, queries_at_risk: null, rollout_sections: [], warning: null },
};

const event = (over: Record<string, unknown>) => ({
  id: `e-${Math.random().toString(16).slice(2, 10)}`,
  at: "2026-11-17T10:41:00Z",
  actor: "jo",
  via: "sso",
  token_id: null,
  category: "catalogs",
  action: "updated",
  resource: null,
  outcome: "succeeded",
  apply_id: null,
  snapshot: null,
  detail: null,
  ...over,
});

/** The trail behind REVIEW_STAGED: every change has an author except events_kafka. */
export const AUDIT = [
  event({ at: "2026-11-17T14:30:00Z", actor: "dana", category: "resource_groups", resource: "global.etl.batch" }),
  event({ at: "2026-11-17T12:07:00Z", actor: "mira", category: "catalogs", action: "removed", resource: "legacy_oracle" }),
  event({ at: "2026-11-17T12:05:00Z", actor: "mira", category: "catalogs", action: "created", resource: "marketing_mysql" }),
  event({ at: "2026-11-17T10:41:00Z", actor: "jo", category: "catalogs", resource: "finance_pg" }),
];

export type Handler = (route: Route, url: URL) => unknown | Promise<unknown>;
export type Stubs = Record<string, unknown | Handler>;

export const DEFAULTS: Stubs = {
  "GET /api/v1/auth/me": { subject: "dana", role: "admin", via: "sso", token_id: null },
  "GET /api/v1/auth/sign-in": { mode: "password" },
  "POST /api/v1/auth/logout": null,
  "GET /api/v1/health": { status: "ok", mongo: "ok" },
  "GET /api/v1/cluster": CLUSTER,
  "GET /api/v1/cluster/probe": PROBE,
  "GET /api/v1/review": REVIEW_STAGED,
  "GET /api/v1/audit": (_route: Route, url: URL) => {
    const q = url.searchParams;
    const items = AUDIT.filter(
      (e) =>
        (!q.get("category") || e.category === q.get("category")) &&
        (!q.get("resource") || e.resource === q.get("resource")) &&
        (!q.get("actor") || e.actor === q.get("actor")) &&
        (!q.get("outcome") || e.outcome === q.get("outcome")),
    );
    return { items, next: null };
  },
  "POST /api/v1/candidate/reset": null,
};

/** A response the handler wants sent as-is, with a status. */
export class Reply {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {}
}

export async function stubApi(page: Page, overrides: Stubs = {}) {
  const stubs: Stubs = { ...DEFAULTS, ...overrides };
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const exact = `${method} ${url.pathname}`;
    // A key may end in `/*` to stand for one path segment, e.g. "GET /api/v1/applies/*".
    const match =
      exact in stubs
        ? exact
        : Object.keys(stubs).find((k) => {
            if (!k.endsWith("/*")) return false;
            const prefix = k.slice(0, -1);
            return exact.startsWith(prefix) && !exact.slice(prefix.length).includes("/");
          });
    if (match === undefined) {
      return route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ code: "not_stubbed", message: `not stubbed: ${exact}`, request_id: "x" }),
      });
    }
    const stub = stubs[match];
    let body = typeof stub === "function" ? await (stub as Handler)(route, url) : stub;
    let status = 200;
    if (body instanceof Reply) {
      status = body.status;
      body = body.body;
    }
    if (body === null) return route.fulfill({ status: status === 200 ? 204 : status, body: "" });
    return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
}
