import createClient from "openapi-fetch";
import type { paths } from "./schema";

/**
 * The one way this app reaches Apchi.
 *
 * `schema.d.ts` is generated from the committed `openapi.json`, which CI regenerates and
 * fails on a diff -- so a contract change breaks the build here rather than at runtime, and
 * §22's "everything in the UI is in the API" is enforced by the type checker instead of by
 * anyone remembering it.
 */
export const api = createClient<paths>({
  // Relative on purpose. The session is an httponly, samesite=lax cookie, so the SPA and the
  // API have to be one origin; an absolute base URL here would be the bug that breaks login.
  baseUrl: "/",
  credentials: "same-origin",
});

export type Schemas = NonNullable<
  paths["/api/v1/review"]["get"]["responses"][200]["content"]["application/json"]
>;

/** What `GET /api/v1/review` answers: the whole Candidate, every Section, and its cost. */
export type Review = Schemas;

/** The cost of applying the Candidate as it stands. */
export type ApplyCost = Review["cost"];

/**
 * Unwraps an openapi-fetch result, raising the API's own message.
 *
 * A failure inside a request is a failure of the thing the caller asked for: returning
 * undefined would make a screen that renders empty and reports success.
 */
export async function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): Promise<T> {
  if (result.error !== undefined || result.data === undefined) {
    const detail =
      typeof result.error === "object" && result.error !== null && "detail" in result.error
        ? String((result.error as { detail: unknown }).detail)
        : `${result.response.status} ${result.response.statusText}`;
    throw new Error(detail);
  }
  return result.data;
}
