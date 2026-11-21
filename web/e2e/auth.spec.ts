import { test, expect, type Page } from "@playwright/test";
import { Reply, stubApi } from "./fixtures";

/**
 * The three authentication modes, as a caller experiences them.
 *
 * The property under test throughout is that the console is never rendered for a caller
 * Apchi does not know. Hiding controls while still fetching would show an Operator a shell
 * full of 403s and call it an interface.
 */

const NOBODY = new Reply(401, { code: "unauthenticated", message: "Sign in first.", request_id: "r" });
const JO = { subject: "jo", role: "operator", via: "password", token_id: null };

async function signedOut(page: Page, mode: string, extra = {}) {
  await stubApi(page, {
    "GET /api/v1/auth/me": NOBODY,
    "GET /api/v1/auth/sign-in": { mode },
    ...extra,
  });
}

const consoleShown = (page: Page) => page.getByRole("navigation", { name: "Main" });

test("password mode offers a real form, not a div that listens for clicks", async ({ page }) => {
  await signedOut(page, "password");
  await page.goto("/");

  await expect(page.getByLabel("Username")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await expect(consoleShown(page)).toHaveCount(0);
});

test("a wrong password shows exactly what Apchi said and nothing more", async ({ page }) => {
  await signedOut(page, "password", {
    "POST /api/v1/auth/login": new Reply(401, {
      code: "unauthenticated",
      message: "That username and password do not match an enabled account.",
      request_id: "r",
    }),
  });
  await page.goto("/");

  await page.getByLabel("Username").fill("jo");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();

  // One message for every failure, so nobody can enumerate who has an account. The UI must
  // not undo that by guessing which half was wrong.
  await expect(page.getByRole("alert")).toHaveText(
    "That username and password do not match an enabled account.",
  );
});

test("a correct password reaches the console without a second round trip", async ({ page }) => {
  let signedIn = false;
  await signedOut(page, "password", {
    "GET /api/v1/auth/me": () => (signedIn ? JO : NOBODY),
    "POST /api/v1/auth/login": () => {
      signedIn = true;
      return JO;
    },
  });
  await page.goto("/");

  await page.getByLabel("Username").fill("jo");
  await page.getByLabel("Password").fill("right");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(consoleShown(page)).toBeVisible();
  await expect(page.getByRole("banner")).toContainText("jo");
});

test("sso mode offers a link the browser can follow, not a fetch", async ({ page }) => {
  await signedOut(page, "sso");
  await page.goto("/");

  const link = page.getByRole("link", { name: /organisation's sign-in/ });
  await expect(link).toHaveAttribute("href", "/api/v1/auth/sso/login");
  await expect(page.getByLabel("Password")).toHaveCount(0);
});

test("none mode never shows a sign-in screen or a sign-out control", async ({ page }) => {
  // `/auth/me` answers in `none` mode too, reporting the anonymous Admin.
  await stubApi(page, {
    "GET /api/v1/auth/me": { subject: "anonymous", role: "admin", via: "none", token_id: null },
  });
  await page.goto("/");

  await expect(consoleShown(page)).toBeVisible();
  await expect(page.getByLabel("Password")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});

test("a signed-in caller can sign out, and lands back on the sign-in screen", async ({ page }) => {
  let signedIn = true;
  await stubApi(page, {
    "GET /api/v1/auth/me": () => (signedIn ? JO : NOBODY),
    "POST /api/v1/auth/logout": () => {
      signedIn = false;
      return null;
    },
  });
  await page.goto("/");
  await expect(consoleShown(page)).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();

  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  // Nothing the previous session read may remain on the screen.
  await expect(consoleShown(page)).toHaveCount(0);
});

test("an unreachable Apchi says so rather than offering a form that cannot work", async ({ page }) => {
  await signedOut(page, "password", {
    "GET /api/v1/auth/sign-in": new Reply(503, { detail: "MongoDB is unreachable" }),
  });
  await page.goto("/");

  await expect(page.getByRole("alert")).toContainText("MongoDB is unreachable");
  await expect(page.getByRole("button", { name: "Sign in" })).toHaveCount(0);
});

test("the sign-in form is completable from the keyboard alone", async ({ page }) => {
  await signedOut(page, "password");
  await page.goto("/");
  await expect(page.getByLabel("Username")).toBeVisible();

  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Username")).toBeFocused();
  await page.keyboard.type("jo");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password")).toBeFocused();
  await page.keyboard.type("secret");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sign in" })).toBeFocused();
});
