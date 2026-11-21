import type { ReactNode } from "react";
import { ApiError } from "../api/client";

/** A read in flight. Says what it is waiting for, so a slow Trino is not a blank page. */
export function Loading({ what }: { what: string }) {
  return (
    <p className="note note--flush muted" role="status">
      Reading {what}…
    </p>
  );
}

/**
 * A refusal or a failure, in Apchi's words, with every problem it named.
 *
 * Shown rather than retried away: a screen that renders empty after a failed read is the
 * failure this codebase refuses everywhere else too.
 */
export function ErrorNote({ error, flush = true }: { error: unknown; flush?: boolean }) {
  if (error === null || error === undefined) return null;
  const message = error instanceof Error ? error.message : String(error);
  const details = error instanceof ApiError ? error.details : [];
  return (
    <div className={`note note--red${flush ? " note--flush" : ""}`} role="alert">
      <div>{message}</div>
      {details.length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {details.map((d, i) => (
            <li key={i}>
              {typeof d.property === "string" && <span className="mono">{d.property}: </span>}
              {typeof d.resource === "string" && <span className="mono">{d.resource}: </span>}
              {String(d.problem ?? d.message ?? JSON.stringify(d))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A page's heading row: title, the sentence under it, and its actions on the right. */
export function PageHead({
  title,
  sub,
  crumb,
  children,
}: {
  title: ReactNode;
  sub?: ReactNode;
  crumb?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="ph">
      <div>
        {crumb !== undefined && <div className="crumb">{crumb}</div>}
        <h1 className="h1">{title}</h1>
        {sub !== undefined && <div className="sub">{sub}</div>}
      </div>
      <span className="sp" />
      {children}
    </div>
  );
}
