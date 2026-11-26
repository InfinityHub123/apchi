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
          after: {
            connector: "postgresql",
            properties: { "connection-url": "jdbc:postgresql://pg-fin-02:5432/finance", "connection-password": "hunter2" },
          },
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
      changes: [
        { resource: "global.etl.batch", change: "changed", before: { hardCpuLimit: null }, after: { hardCpuLimit: "4h" } },
        // No audit event names it, so its author is not known.
        { resource: "global.adhoc", change: "added", before: null, after: { hardConcurrencyLimit: 20 } },
      ],
    },
  ],
  cost: {
    restarts_coordinator: true,
    queries_at_risk: 14,
    rollout_sections: ["certificate_mapping", "resource_groups"],
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

const snapshot = (number: number, created_at: string, apply_id: string) => ({
  number,
  created_at,
  apply_id,
  sections: {},
});

export const SNAPSHOTS = [
  snapshot(13, SNAPSHOT_AT, "a-7f3c"),
  snapshot(12, "2026-11-06T10:15:00Z", "a-71b0"),
  snapshot(11, "2026-11-03T09:40:00Z", "a-6c22"),
  snapshot(10, "2026-10-28T16:12:00Z", "a-6a07"),
];

const cert = (name: string, not_after: string, status: string) => ({
  name,
  common_name: `${name}.acme.internal`,
  subject: `CN=${name}.acme.internal`,
  issuer: "CN=Acme Internal Issuing CA",
  not_before: "2026-01-01T00:00:00Z",
  not_after,
  status,
  expiring_within_days: 30,
  fingerprint: "ab".repeat(32),
});

export const CERTIFICATES = [
  cert("finance_pg_tls", "2027-03-14T09:12:07Z", "valid"),
  cert("search_tls", "2026-12-09T00:00:00Z", "expiring"),
];

export const VALIDATIONS = [
  {
    id: "v-3190",
    base_snapshot: 13,
    started_at: "2026-11-17T11:00:00Z",
    finished_at: "2026-11-17T11:02:20Z",
    outcome: "passed",
    failures: [],
    warnings: [],
  },
];

const rules = (name: string, over: Record<string, unknown> = {}) => ({
  name,
  required: [],
  optional: [],
  branches: [],
  open_ended: [],
  prefixes: [],
  secrets: [],
  ...over,
});

export const CONNECTORS = [
  rules("postgresql", { required: ["connection-url"], optional: ["connection-user", "connection-password"], secrets: ["connection-password"] }),
  rules("iceberg", {
    optional: ["iceberg.file-format"],
    branches: [
      {
        selector: "iceberg.catalog.type",
        default: "hive_metastore",
        allowed: ["hive_metastore", "rest", "glue"],
        values: { hive_metastore: ["hive.metastore.uri"], rest: ["iceberg.rest-catalog.uri"], glue: [] },
      },
    ],
    prefixes: ["fs."],
  }),
];

export const CATALOGS = [
  {
    name: "finance_pg",
    connector: "postgresql",
    supported: true,
    certificate: "finance_pg_tls",
    properties: { "connection-url": "jdbc:postgresql://pg-fin-02:5432/finance", "connection-password": "hunter2" },
  },
  { name: "marketing_mysql", connector: "mysql", supported: false, certificate: null, properties: {} },
  { name: "lake_iceberg", connector: "iceberg", supported: true, certificate: null, properties: { "iceberg.catalog.type": "rest", "iceberg.rest-catalog.uri": "https://rest" } },
];
export const LISTENER_TYPES = [
  rules("http", { required: ["http-event-listener.connect-ingest-uri"], optional: ["http-event-listener.log-completed"], secrets: [] }),
  rules("kafka", { required: ["kafka-event-listener.broker-endpoints"], optional: ["kafka-event-listener.client-id"], secrets: [] }),
];

export const GRANTS = [
  { key: "analyst_ro:finance_pg:reporting:*", identity: "analyst_ro", catalog: "finance_pg", schema: "reporting", table: null, privileges: ["SELECT"] },
  { key: "analyst_ro:lake_iceberg:*:*", identity: "analyst_ro", catalog: "lake_iceberg", schema: null, table: null, privileges: ["SELECT"] },
  { key: "etl:lake_iceberg:raw:*", identity: "etl", catalog: "lake_iceberg", schema: "raw", table: null, privileges: ["SELECT", "INSERT"] },
];

export const GROUPS = [
  { path: "global", hard_concurrency_limit: 100, soft_memory_limit: "100%" },
  { path: "global.etl", hard_concurrency_limit: 10, soft_memory_limit: "60%" },
  { path: "global.etl.batch", hard_concurrency_limit: 6, soft_memory_limit: "50GB", hard_cpu_limit: "4h" },
];

export const SELECTORS = [
  { group: "global.dashboards", source: "^(superset|tableau)$" },
  { group: "global.etl.batch", user: "^svc_etl_.*", query_type: "INSERT" },
  { group: "global.adhoc", user_group: "analysts" },
  { group: "global.etl", user: "^svc_.*" },
];


const NO_PROPERTIES = { "config.properties": {}, "node.properties": {}, "log.properties": {} };

/** As the API returns it: both roles, every file, values in full. */
export const SERVER_CONFIGURATION = {
  coordinator: {
    ...NO_PROPERTIES,
    "event-listener.properties": {},
    "config.properties": {
      "query.max-execution-time": "2h",
      "internal-communication.shared-secret": "s3cret-shared",
    },
  },
  worker: { ...NO_PROPERTIES, "config.properties": { "task.concurrency": "16" } },
};

/** As the next Apply would write them: wiring first, then the Admin's. */
export const RENDERED_FILES = {
  coordinator: {
    "/etc/trino/config.properties":
      "coordinator=true\nhttp-server.http.port=8080\ninternal-communication.shared-secret=s3cret-shared\nquery.max-execution-time=2h\n",
    "/etc/trino/log.properties": "",
  },
  worker: {
    "/etc/trino/config.properties": "coordinator=false\nshutdown.grace-period=120s\ntask.concurrency=16\n",
  },
};

export const DRIFT = {
  at: NOW,
  snapshot: 13,
  diverged: true,
  sections: [
    { section: "catalogs", status: "not_comparable", restarts_coordinator: false },
    { section: "client_certificates", status: "in_sync", restarts_coordinator: false },
    { section: "certificate_mapping", status: "diverged", restarts_coordinator: true },
    { section: "permissions", status: "in_sync", restarts_coordinator: false },
    { section: "resource_groups", status: "unknown", restarts_coordinator: true },
  ],
};

/** A response the handler wants sent as-is, with a status. */
export class Reply {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {}
}

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
        (!q.get("outcome") || e.outcome === q.get("outcome")) &&
        (!q.get("since") || e.at >= q.get("since")!),
    );
    return { items, next: null };
  },
  "POST /api/v1/candidate/reset": null,
  "POST /api/v1/candidate/reset-resource": null,
  "GET /api/v1/snapshots": { items: SNAPSHOTS, next: null },
  "GET /api/v1/certificates": CERTIFICATES,
  "GET /api/v1/validations": { items: VALIDATIONS, next: null },
  "GET /api/v1/catalogs/connectors": CONNECTORS,
  "GET /api/v1/admin/server-configuration/event-listener-types": LISTENER_TYPES,
  "GET /api/v1/cluster/drift": DRIFT,
  "GET /api/v1/catalogs": CATALOGS,
  "GET /api/v1/permissions": GRANTS,
  "GET /api/v1/permissions/system": {
    rules: [{ rule: "Apchi's verification user may query the system catalog", why: "Verification runs a real query after every Apply." }],
  },
  "GET /api/v1/resource-groups": GROUPS,
  "GET /api/v1/resource-groups/selectors": { selectors: SELECTORS },
  "GET /api/v1/resource-groups/settings": { cpu_quota_period: null },
  "GET /api/v1/certificate-mapping": new Reply(404, { code: "not_found", message: "No pattern.", request_id: "r" }),
  "GET /api/v1/admin/certificate-mapping/preserved": { patterns: [] },
  "GET /api/v1/admin/server-configuration": SERVER_CONFIGURATION,
  "GET /api/v1/admin/server-configuration/files": RENDERED_FILES,
  "GET /api/v1/catalogs/*": (_route: Route, url: URL) =>
    CATALOGS.find((c) => url.pathname.endsWith(`/${c.name}`)) ?? new Reply(404, { code: "not_found", message: "No such catalog.", request_id: "r" }),
};

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
    // A handler that answered the route itself (a stream, say) returns nothing.
    if (body === undefined) return;
    let status = 200;
    if (body instanceof Reply) {
      status = body.status;
      body = body.body;
    }
    if (body === null) return route.fulfill({ status: status === 200 ? 204 : status, body: "" });
    return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
}
