import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export type StreamEvent = { id: string; stage: string; at: string; detail: string | null };

/**
 * An Apply's stage transitions as they happen, from its server-sent event stream.
 *
 * `EventSource` reconnects by itself and sends `Last-Event-ID`, and the server replays
 * everything after it, so a dropped connection resumes rather than leaving a gap. Each
 * event also refreshes the Apply's record, which carries what the stream does not --
 * Verification's checks, warnings, the Snapshot it committed.
 */
export function useApplyStream(id: string | null, finished: boolean) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const queries = useQueryClient();

  useEffect(() => {
    setEvents([]);
    // A finished Apply's stream ends at once, and `EventSource` would reconnect to it every
    // few seconds forever; its record already holds the whole history.
    if (id === null || finished) return;
    const source = new EventSource(`/api/v1/applies/${encodeURIComponent(id)}/events`);
    source.onopen = () => setConnected(true);
    source.addEventListener("stage", (e) => {
      const msg = e as MessageEvent<string>;
      const data = JSON.parse(msg.data) as Omit<StreamEvent, "id">;
      setEvents((prev) =>
        prev.some((p) => p.id === msg.lastEventId) ? prev : [...prev, { ...data, id: msg.lastEventId }],
      );
      void queries.invalidateQueries({ queryKey: ["apply", id] });
      void queries.invalidateQueries({ queryKey: ["cluster"] });
    });
    // The server closes the stream when the Apply ends; the record refetch below is what
    // turns `finished` true and closes this for good.
    source.onerror = () => {
      setConnected(false);
      void queries.invalidateQueries({ queryKey: ["apply", id] });
    };
    return () => source.close();
  }, [id, finished, queries]);

  return { events, connected };
}
