import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, done, unwrap, type S } from "../api/client";

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

/** Trino's own files: the Admin's properties for each role, rendered with the chart's wiring. */
export type ServerConfiguration = S["ServerConfiguration"];
export type Role = "coordinator" | "worker";
export const FILES = ["config.properties", "node.properties", "log.properties"] as const;
export type FileName = (typeof FILES)[number];
export type RoleFiles = Record<FileName, Record<string, string>>;

/** Every role and file present, empty where nothing is set -- the shape the screen edits. */
export function filled(configuration: ServerConfiguration | undefined): Record<Role, RoleFiles> {
  const role = (r: Role): RoleFiles => {
    const files = (configuration?.[r] ?? {}) as Partial<Record<FileName, Record<string, string>>>;
    return Object.fromEntries(FILES.map((f) => [f, { ...(files[f] ?? {}) }])) as RoleFiles;
  };
  return { coordinator: role("coordinator"), worker: role("worker") };
}

export function useServerConfiguration() {
  return useQuery({
    queryKey: ["admin", "server-configuration"],
    queryFn: () => api.GET("/api/v1/admin/server-configuration").then(unwrap),
  });
}

export function useRenderedFiles() {
  return useQuery({
    queryKey: ["admin", "server-configuration", "files"],
    queryFn: () => api.GET("/api/v1/admin/server-configuration/files").then(unwrap),
  });
}

/** Replaces every property of both roles, as the PUT does. */
export function saveServerConfiguration(configuration: Record<Role, RoleFiles>) {
  return api.PUT("/api/v1/admin/server-configuration", { body: configuration }).then(unwrap);
}
