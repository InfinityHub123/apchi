import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, done, unwrap } from "../api/client";

/**
 * Raw files are addressed by their absolute path, slashes and all, which the server takes
 * as a path parameter. Built by hand rather than through openapi-fetch, which would encode
 * the slashes into one opaque segment.
 */
export const rawFileUrl = (path: string) => `/api/v1/admin/files/${path.replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/")}`;

export function useRawFiles() {
  return useQuery({ queryKey: ["admin", "files"], queryFn: () => api.GET("/api/v1/admin/files").then(unwrap) });
}

export function useEnforcement() {
  return useQuery({ queryKey: ["admin", "enforcement"], queryFn: () => api.GET("/api/v1/admin/permissions/enforcement").then(unwrap) });
}

/** Admin Apply: the latest Snapshot plus today's Admin values -- never the Candidate. */
export function useAdminApply() {
  const navigate = useNavigate();
  const queries = useQueryClient();
  return useMutation({
    mutationFn: () => api.POST("/api/v1/admin/applies").then(unwrap),
    onSuccess: (record) => {
      void queries.invalidateQueries({ queryKey: ["cluster"] });
      navigate(`/apply?id=${record.id}`);
    },
  });
}

export async function sendRawFile(method: "PUT" | "DELETE", path: string, content?: string) {
  const response = await fetch(rawFileUrl(path), {
    method,
    credentials: "same-origin",
    headers: content === undefined ? undefined : { "Content-Type": "application/json" },
    body: content === undefined ? undefined : JSON.stringify({ content }),
  });
  // Reuse the one place that turns an error body into Apchi's message.
  const body: unknown = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) await done({ error: body ?? {}, response });
}
