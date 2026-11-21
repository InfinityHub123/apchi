import type { RouteObject } from "react-router-dom";
import { NotBuilt } from "./screens/NotBuilt";

/**
 * Every screen, by the path it lives at. One table, so a link anywhere in the console and
 * the sidebar cannot disagree about where a screen is.
 */
export const routes: RouteObject[] = [
  { index: true, element: <NotBuilt title="Overview" /> },
  { path: "review", element: <NotBuilt title="Staged changes" /> },
  { path: "apply", element: <NotBuilt title="Apply" /> },
  { path: "catalogs/*", element: <NotBuilt title="Catalogs" /> },
  { path: "certificates", element: <NotBuilt title="Client certificates" /> },
  { path: "certificate-mapping", element: <NotBuilt title="Certificate mapping" /> },
  { path: "permissions", element: <NotBuilt title="Permissions" /> },
  { path: "resource-groups", element: <NotBuilt title="Resource groups" /> },
  { path: "event-listeners/*", element: <NotBuilt title="Event listeners" /> },
  { path: "snapshots", element: <NotBuilt title="Snapshots" /> },
  { path: "drift", element: <NotBuilt title="Drift" /> },
  { path: "applies/*", element: <NotBuilt title="Applies" /> },
  { path: "validations", element: <NotBuilt title="Validations" /> },
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
