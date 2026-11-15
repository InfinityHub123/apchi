import { test, expect } from "@playwright/test";
import { stubApi, REVIEW_STAGED } from "./fixtures";

/**
 * The three authentication modes, as a caller experiences them.
 *
 * The property under test throughout is that the console is never rendered for a caller
 * Apchi does not know. Hiding controls while still fetching would show an Operator a shell
 * full of 403s and call it an interface.
 */

/** A caller with no session: `/auth/me` refuses, and the mode decides what they are offered. */
async function signedOut(page: import("@playwright/test").Page, mode: string) {
  await stubApi(page, { caller: null });
  await page.route("**/api/v1/auth/sign-in", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode }),
    }),
  );
}

test("password mode offers a real form, not a div that listens for clicks", async ({ page }) => {
  await signedOut(page, "password");
  await page.goto("/");

  await expect(page.getByLabel("Username")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  // The console is not behind it, merely hidden.
  await expect(page.getByText("Candidate", { exact: true })).toHaveCount(0);
});

test("a wrong password shows exactly what Apchi said and nothing more", async ({ page }) => {
  await signedOut(page, "password");
  await page.route("**/api/v1/auth/login", (r) =>
    r.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "That username and password do not match an enabled account.",
      }),
    }),
  );
  await page.goto("/");

  await page.getByLabel("Username").fill("jo");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in" }).click();

  // Apchi answers one message for every failure -- unknown username, wrong password,
  // disabled account -- so nobody can enumerate the platform staff who have accounts. The UI
  // must not undo that by adding a guess about which half was wrong, so it shows that
  // message and exactly that message.
  await expect(page.getByRole("alert")).toHaveText(
    "That username and password do not match an enabled account.",
  );
});

test("a correct password reaches the console without a second round trip", async ({ page }) => {
  await signedOut(page, "password");
  await page.route("**/api/v1/auth/login", async (r) => {
    // From here on the caller has a session, so /auth/me must stop refusing.
    await page.route("**/api/v1/auth/me", (m) =>
      m.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ subject: "jo", role: "operator", via: "password" }),
      }),
    );
    await r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ subject: "jo", role: "operator", via: "password" }),
    });
  });
  await page.route("**/api/v1/review", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(REVIEW_STAGED),
    }),
  );
  await page.goto("/");

  await page.getByLabel("Username").fill("jo");
  await page.getByLabel("Password").fill("right");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByText("Candidate", { exact: true })).toBeVisible();
  await expect(page.getByRole("banner")).toContainText("jo");
});

test("sso mode offers a link the browser can follow, not a fetch", async ({ page }) => {
  await signedOut(page, "sso");
  await page.goto("/");

  const link = page.getByRole("link", { name: /single sign-on/i });
  await expect(link).toHaveAttribute("href", "/api/v1/auth/sso/login");
  await expect(page.getByLabel("Password")).toHaveCount(0);
});

test("none mode never shows a sign-in screen or a sign-out control", async ({ page }) => {
  // `/auth/me` answers in `none` mode too, reporting the anonymous Admin, so this caller is
  // known and goes straight to the console.
  await stubApi(page, { caller: { subject: "nobody", role: "admin", via: "none" } });
  await page.goto("/");

  await expect(page.getByText("Candidate", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Password")).toHaveCount(0);
  // There is no session to end, so offering the control would be a button that does nothing.
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});

test("a signed-in caller can sign out, and lands back on the sign-in screen", async ({ page }) => {
  await stubApi(page, { caller: { subject: "jo", role: "operator", via: "password" } });
  await page.route("**/api/v1/auth/sign-in", (r) =>
    r.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ mode: "password" }),
    }),
  );
  await page.goto("/");

  await expect(page.getByText("Candidate", { exact: true })).toBeVisible();

  await page.route("**/api/v1/auth/logout", async (r) => {
    await page.route("**/api/v1/auth/me", (m) =>
      m.fulfill({ status: 401, contentType: "application/json", body: "{}" }),
    );
    await r.fulfill({ status: 204, body: "" });
  });

  await page.getByRole("button", { name: "Sign out" }).click();

  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  // Nothing the previous session read may remain on the screen.
  await expect(page.getByText("Candidate", { exact: true })).toHaveCount(0);
});

test("an unreachable Apchi says so rather than offering a form that cannot work", async ({
  page,
}) => {
  await stubApi(page, { caller: null });
  await page.route("**/api/v1/auth/sign-in", (r) =>
    r.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "MongoDB is unreachable" }),
    }),
  );
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
