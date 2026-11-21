import { ApiError } from "../api/client";

/**
 * Fetch an export and hand it to the browser as a file.
 *
 * Fetched rather than linked, so a refusal (403 for an Operator asking for the Admin export,
 * say) arrives as Apchi's message on the page instead of a browser download of an error body.
 */
export async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { message?: string; detail?: string };
      message = body.message ?? body.detail ?? message;
    } catch {
      // Not JSON: the status line is the most specific thing there is.
    }
    throw new ApiError(message, response.status, null, []);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
