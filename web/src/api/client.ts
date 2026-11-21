import createClient from "openapi-fetch";
import type { components, paths } from "./schema";

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

export type S = components["schemas"];
export type Review = S["Review"];
export type ApplyCost = S["ApplyCost"];
export type SectionName = S["SectionDiff"]["section"];

/**
 * A refusal, carrying what Apchi said.
 *
 * `details` is kept because the API names every problem rather than the first -- an invalid
 * catalog answers one entry per property -- and a form that showed only `message` would make
 * somebody fix them one round trip at a time.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null,
    readonly details: Array<Record<string, unknown>>,
  ) {
    super(message);
  }
}

function asApiError(error: unknown, response: Response): ApiError {
  const body = (typeof error === "object" && error !== null ? error : {}) as Record<
    string,
    unknown
  >;
  // `message` is Apchi's own envelope; `detail` is what FastAPI answers for the few refusals
  // that never reach Apchi's handlers. Both are prose meant for a person.
  const message =
    typeof body.message === "string"
      ? body.message
      : typeof body.detail === "string"
        ? body.detail
        : `${response.status} ${response.statusText}`.trim();
  return new ApiError(
    message,
    response.status,
    typeof body.code === "string" ? body.code : null,
    Array.isArray(body.details) ? (body.details as Array<Record<string, unknown>>) : [],
  );
}

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
  if (result.error !== undefined || !result.response.ok) {
    throw asApiError(result.error, result.response);
  }
  return result.data as T;
}

/** For the 204s: the request succeeded or it raised. */
export async function done(result: { error?: unknown; response: Response }): Promise<void> {
  if (result.error !== undefined || !result.response.ok) {
    throw asApiError(result.error, result.response);
  }
}

/**
 * A multipart POST, for the two certificate routes that take a ZIP. Outside openapi-fetch
 * because its body serialiser is JSON; the response is unwrapped the same way, so a refusal
 * reads the same here as anywhere else.
 */
export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(path, { method: "POST", body: form, credentials: "same-origin" });
  const text = await response.text();
  const body: unknown = text === "" ? undefined : JSON.parse(text);
  if (!response.ok) throw asApiError(body ?? {}, response);
  return body as T;
}
