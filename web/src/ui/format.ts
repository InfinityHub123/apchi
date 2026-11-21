/**
 * How times and sizes read on screen.
 *
 * One formatter so "11 Nov 14:02" means the same thing in the top bar, the audit and an
 * Apply's history. Local time, because the person reading is matching it against a clock on
 * their own wall; the ISO instant stays in a `title` for anyone correlating with logs.
 */

const DAY_TIME = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const DATE = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const CLOCK_S = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const at = (iso: string) => new Date(iso);

export const dayTime = (iso: string) => DAY_TIME.format(at(iso));
export const date = (iso: string) => DATE.format(at(iso));
export const dateTime = (iso: string) => DATE_TIME.format(at(iso));
export const clock = (iso: string) => CLOCK.format(at(iso));
export const clockSeconds = (iso: string) => CLOCK_S.format(at(iso));

/** "3m 23s", "2h 4m", "41s": how long something took, or has been taking. */
export function duration(fromIso: string, toIso?: string | null): string {
  const ms = (toIso ? at(toIso) : new Date()).getTime() - at(fromIso).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Whole days from now until `iso`; negative once it has passed. */
export function daysUntil(iso: string): number {
  return Math.floor((at(iso).getTime() - Date.now()) / 86_400_000);
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export const plural = (n: number, one: string, many = `${one}s`) =>
  `${n} ${n === 1 ? one : many}`;
