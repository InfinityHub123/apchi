import { defineConfig, devices } from "@playwright/test";

/**
 * The UI is tested against the built bundle, not the dev server, because the build is what
 * ships and a Vite transform that only works in development is a real failure mode.
 *
 * The API is stubbed per test from the committed contract's own shapes rather than reached
 * for real: these tests are about what the UI does with an answer, and a MongoDB plus a Trino
 * to produce one belongs to the Python tiers that already have them.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "npx vite preview --port 4173 --strictPort --host 127.0.0.1",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
  },
});
