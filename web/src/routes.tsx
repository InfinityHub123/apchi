import type { RouteObject } from "react-router-dom";
import { NotBuilt } from "./screens/NotBuilt";
import { OverviewScreen } from "./screens/Overview";
import { ReviewScreen } from "./screens/Review";
import { DriftScreen } from "./screens/Drift";
import { ApplyScreen } from "./screens/Apply";
import { AppliesScreen } from "./screens/Applies";
import { ValidationsScreen } from "./screens/Validations";
import { CatalogsScreen } from "./screens/Catalogs";
import { CertificatesScreen } from "./screens/Certificates";
import { CertMappingScreen } from "./screens/CertMapping";

/**
 * Every screen, by the path it lives at. One table, so a link anywhere in the console and
 * the sidebar cannot disagree about where a screen is.
 */
export const routes: RouteObject[] = [
  { index: true, element: <OverviewScreen /> },
  { path: "review", element: <ReviewScreen /> },
  { path: "apply", element: <ApplyScreen /> },
  { path: "catalogs", element: <CatalogsScreen /> },
  { path: "catalogs/:name", element: <CatalogsScreen /> },
  { path: "catalogs/:name/:action", element: <CatalogsScreen /> },
  { path: "certificates", element: <CertificatesScreen /> },
  { path: "certificate-mapping", element: <CertMappingScreen /> },
  { path: "permissions", element: <NotBuilt title="Permissions" /> },
  { path: "resource-groups", element: <NotBuilt title="Resource groups" /> },
  { path: "event-listeners/*", element: <NotBuilt title="Event listeners" /> },
  { path: "snapshots", element: <NotBuilt title="Snapshots" /> },
  { path: "drift", element: <DriftScreen /> },
  { path: "applies", element: <AppliesScreen /> },
  { path: "applies/:id", element: <AppliesScreen /> },
  { path: "validations", element: <ValidationsScreen /> },
  { path: "audit", element: <NotBuilt title="Audit" /> },
  { path: "import-export", element: <NotBuilt title="Import / export" /> },
  { path: "admin", element: <NotBuilt title="Admin values" /> },
  { path: "admin/enforcement", element: <NotBuilt title="Permission enforcement" /> },
  { path: "admin/raw-files", element: <NotBuilt title="Raw files" /> },
  { path: "admin/adoption", element: <NotBuilt title="Adoption" /> },
  { path: "maintenance", element: <NotBuilt title="Maintenance Mode" /> },
  { path: "tokens", element: <NotBuilt title="API tokens" /> },
  { path: "*", element: <NotBuilt title="Not found" /> },
];
