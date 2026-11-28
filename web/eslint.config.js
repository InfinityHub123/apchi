// typescript-eslint's recommended rules over the app and its browser tests. The client types
// are generated from openapi.json, and CI already checks they match it, so they are not
// linted here.
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "test-results", "playwright-report", "src/api/schema.d.ts"] },
  ...tseslint.configs.recommended,
);
