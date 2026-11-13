/**
 * Field names whose values are hidden until an Operator asks to see them.
 *
 * **This is not a security boundary.** §12 is explicit: "All Apchi users -- Operators and
 * Admins -- are trusted, so there is no requirement to hide secret values from them", and the
 * API returns them for exactly that reason. Masking here is ergonomics: a diff is read during
 * a screen-share and over a shoulder, and a credential that only appears when somebody asks
 * for it is one that does not appear by accident. Anyone who can see the mask can reveal it,
 * and could read the same value straight from the API.
 *
 * Mirrored from `_SECRET_NAMES` in `app/logging.py`, which is the list the log redaction
 * filter uses. Two copies of a list is a drift risk and this is the second one -- it is here
 * rather than fetched because the names are not data the API publishes, and because a UI that
 * could not mask until a request returned would flash the values first.
 */
const SECRET_NAMES = [
  "password",
  "passwd",
  "secret",
  "token",
  "credential",
  "private_key",
  "privatekey",
  "ssl_key",
  "sslkey",
  "connection-password",
  "connection_password",
  "authorization",
] as const;

export function isSecretName(name: string): boolean {
  const lowered = name.toLowerCase();
  return SECRET_NAMES.some((s) => lowered.includes(s));
}

/** What is shown in place of a value until it is revealed. Not the value's own length. */
export const MASK = "••••••••";
