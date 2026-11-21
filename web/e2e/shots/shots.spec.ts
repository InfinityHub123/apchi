import { test } from "@playwright/test";
import { stubApi } from "../fixtures";

/**
 * Not a test: screenshots of each screen against stubbed data, to compare with the design
 * canvas by eye. Skipped unless SHOTS names a directory to write them to.
 */
const dir = process.env.SHOTS;
test.skip(!dir, "SHOTS not set");

const screens = (process.env.SHOT_PATHS ?? "/").split(",");

for (const path of screens) {
  test(`shot ${path}`, async ({ page }) => {
    await page.setViewportSize({ width: Number(process.env.SHOT_W ?? 1440), height: 1040 });
    await stubApi(page);
    await page.goto(path);
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${dir}/${path.replace(/\W+/g, "_") || "root"}.png`, fullPage: true });
  });
}
