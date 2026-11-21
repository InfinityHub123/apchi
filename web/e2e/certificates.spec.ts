import { test, expect, type Route } from "@playwright/test";
import { CERTIFICATES, Reply, stubApi } from "./fixtures";

/** Client certificates (outbound) and certificate mapping (inbound). */

const READING = {
  certificate_member: "search/tls.crt",
  key_member: "search/tls.key",
  conversions: ["The private key is PKCS#1; it will be stored as PKCS#8."],
  certificate: { ...CERTIFICATES[1], not_after: "2027-12-08T00:00:00Z", status: "valid" },
  replaces: CERTIFICATES[1],
  used_by: ["search_es"],
};

async function chooseZip(page: import("@playwright/test").Page) {
  await page.getByRole("textbox", { name: /^Name/ }).fill("search_tls");
  await page.getByLabel("Certificate archive").setInputFiles({ name: "search.zip", mimeType: "application/zip", buffer: Buffer.from("PK") });
}

test("the listing shows expiry, status and which Catalogs present each certificate", async ({ page }) => {
  await stubApi(page);
  await page.goto("/certificates");
  await expect(page.getByRole("row", { name: /finance_pg_tls/ })).toContainText("finance_pg");
  await expect(page.getByRole("row", { name: /search_tls/ })).toContainText("Expiring");
  await expect(page.getByText("Expiring within 30 days")).toBeVisible();
});

test("an upload is read first, shows what it found, and stages only on confirmation", async ({ page }) => {
  const calls: string[] = [];
  await stubApi(page, {
    "POST /api/v1/certificates/inspect": () => {
      calls.push("inspect");
      return READING;
    },
    "POST /api/v1/certificates": (route: Route) => {
      calls.push("upload");
      return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify(READING.certificate) });
    },
  });
  await page.goto("/certificates");
  await chooseZip(page);
  await page.getByRole("button", { name: "Read the archive" }).click();
  const read = page.getByRole("region", { name: "What Apchi read" });
  await expect(read).toContainText("Would replace");
  await expect(read).toContainText("search/tls.crt");
  await expect(read).toContainText("PKCS#1");
  await expect(read).toContainText("search_es");
  expect(calls).toEqual(["inspect"]);
  await read.getByRole("button", { name: "Stage the replacement" }).click();
  await expect.poll(() => calls).toEqual(["inspect", "upload"]);
});

test("a refusal to read the archive is shown in Apchi's words", async ({ page }) => {
  await stubApi(page, {
    "POST /api/v1/certificates/inspect": new Reply(422, {
      code: "unprocessable_payload",
      message: "The ZIP archive holds no private key Apchi can read.",
      request_id: "r",
    }),
  });
  await page.goto("/certificates");
  await chooseZip(page);
  await page.getByRole("button", { name: "Read the archive" }).click();
  await expect(page.getByText("The ZIP archive holds no private key Apchi can read.")).toBeVisible();
});

test("the mapping tester derives an identity in the browser, anchored to the whole subject", async ({ page }) => {
  await stubApi(page, {
    "GET /api/v1/certificate-mapping": { pattern: "CN=([^,]+), OU=analytics.*", user: "$1", case: "lower" },
    "GET /api/v1/admin/certificate-mapping/preserved": { patterns: [{ pattern: "CN=([^,]+),OU=legacy.*", user: "$1", case: "keep" }] },
  });
  await page.goto("/certificate-mapping");
  await expect(page.getByRole("row", { name: /jo.levi/ })).toContainText("jo.levi");
  await page.getByLabel("Subject to try").fill("CN=Mira, OU=finance, O=Acme");
  await page.getByRole("button", { name: "Try subject" }).click();
  await expect(page.getByRole("row", { name: /CN=Mira/ })).toContainText("no match — falls through to preserved patterns");
  await page.getByLabel("Regular expression").fill("CN=([^,]+");
  await expect(page.getByText("invalid pattern").first()).toBeVisible();
  await expect(page.getByText("CN=([^,]+),OU=legacy.*")).toBeVisible();
});

test("staging a mapping sends the pattern, and says it restarts the coordinator", async ({ page }) => {
  let sent: unknown = null;
  await stubApi(page, {
    "GET /api/v1/certificate-mapping": new Reply(404, { code: "not_found", message: "No pattern.", request_id: "r" }),
    "GET /api/v1/admin/certificate-mapping/preserved": { patterns: [] },
    "PUT /api/v1/certificate-mapping": (route: Route) => {
      sent = route.request().postDataJSON();
      return sent;
    },
  });
  await page.goto("/certificate-mapping");
  await expect(page.getByText(/No pattern\. Certificate authentication/)).toBeVisible();
  await expect(page.getByText("restarts the coordinator")).toBeVisible();
  await page.getByLabel("Regular expression").fill("CN=([^,]+).*");
  await page.getByRole("radio", { name: "UPPER" }).click();
  await page.getByRole("button", { name: "Stage change" }).click();
  await expect.poll(() => sent).toEqual({ pattern: "CN=([^,]+).*", user: "$1", case: "upper" });
});
