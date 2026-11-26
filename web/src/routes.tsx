import type { RouteObject } from "react-router-dom";

/**
 * Every screen, by the path it lives at. One table, so a link anywhere in the console and
 * the sidebar cannot disagree about where a screen is.
 *
 * Each screen is its own chunk, loaded when first visited: an Operator who opens the
 * Overview should not download the Admin screens first.
 */
const screen = (load: () => Promise<object>, name: string): Pick<RouteObject, "lazy"> => ({
  lazy: async () => {
    const Component = (await load() as Record<string, React.ComponentType | undefined>)[name];
    if (Component === undefined) throw new Error(`no screen named ${name}`);
    return { Component };
  },
});

const overview = screen(() => import("./screens/Overview"), "OverviewScreen");
const catalogs = screen(() => import("./screens/Catalogs"), "CatalogsScreen");
const applies = screen(() => import("./screens/Applies"), "AppliesScreen");

export const routes: RouteObject[] = [
  { index: true, ...overview },
  { path: "review", ...screen(() => import("./screens/Review"), "ReviewScreen") },
  { path: "apply", ...screen(() => import("./screens/Apply"), "ApplyScreen") },
  { path: "catalogs", ...catalogs },
  { path: "catalogs/:name", ...catalogs },
  { path: "catalogs/:name/:action", ...catalogs },
  { path: "certificates", ...screen(() => import("./screens/Certificates"), "CertificatesScreen") },
  { path: "certificate-mapping", ...screen(() => import("./screens/CertMapping"), "CertMappingScreen") },
  { path: "permissions", ...screen(() => import("./screens/Permissions"), "PermissionsScreen") },
  { path: "resource-groups", ...screen(() => import("./screens/ResourceGroups"), "ResourceGroupsScreen") },
  { path: "snapshots", ...screen(() => import("./screens/Snapshots"), "SnapshotsScreen") },
  { path: "drift", ...screen(() => import("./screens/Drift"), "DriftScreen") },
  { path: "applies", ...applies },
  { path: "applies/:id", ...applies },
  { path: "validations", ...screen(() => import("./screens/Validations"), "ValidationsScreen") },
  { path: "audit", ...screen(() => import("./screens/Audit"), "AuditScreen") },
  { path: "import-export", ...screen(() => import("./screens/ImportExport"), "ImportExportScreen") },
  { path: "admin", ...screen(() => import("./screens/AdminValues"), "AdminValuesScreen") },
  { path: "admin/enforcement", ...screen(() => import("./screens/Enforcement"), "EnforcementScreen") },
  { path: "admin/raw-files", ...screen(() => import("./screens/RawFiles"), "RawFilesScreen") },
  {
    path: "admin/server-configuration",
    ...screen(() => import("./screens/ServerConfiguration"), "ServerConfigurationScreen"),
  },
  { path: "maintenance", ...screen(() => import("./screens/Maintenance"), "MaintenanceScreen") },
  { path: "tokens", ...screen(() => import("./screens/Tokens"), "TokensScreen") },
  { path: "*", ...screen(() => import("./screens/NotFound"), "NotFound") },
];
