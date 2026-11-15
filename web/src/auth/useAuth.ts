import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import type { components } from "../api/schema";

export type Identity = components["schemas"]["Identity"];
export type AuthMode = components["schemas"]["AuthMode"];

/**
 * Who the caller is, or 401 when they are nobody yet.
 *
 * `/auth/me` answers in every mode including `none`, where it reports the anonymous Admin,
 * so this is one question rather than a branch on configuration. A 401 is a normal answer
 * here and not an error worth retrying into a loop.
 */
export function useIdentity() {
  return useQuery({
    queryKey: ["identity"],
    queryFn: () => api.GET("/api/v1/auth/me").then(unwrap),
    retry: false,
    // A session can expire while a tab is open, and the next read would 403 with no
    // explanation. Rechecking on focus turns that into the login screen instead.
    refetchOnWindowFocus: true,
  });
}

/**
 * How to sign in here.
 *
 * Fetched only once the identity query has failed, because in `none` mode and for an
 * already-signed-in caller there is nothing to ask.
 */
export function useSignIn(enabled: boolean) {
  return useQuery({
    queryKey: ["sign-in"],
    queryFn: () => api.GET("/api/v1/auth/sign-in").then(unwrap),
    enabled,
    staleTime: Infinity,
  });
}

export function useLogin() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: (credentials: { username: string; password: string }) =>
      api.POST("/api/v1/auth/login", { body: credentials }).then(unwrap),
    onSuccess: (identity) => {
      // Seed rather than invalidate: the response *is* the answer /auth/me would give, and
      // a refetch here would show the console for a frame with no identity in it.
      queries.setQueryData(["identity"], identity);
      void queries.invalidateQueries();
    },
  });
}

export function useLogout() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: () => api.POST("/api/v1/auth/logout", {}),
    onSettled: () => {
      // Everything read while signed in belonged to that session, so none of it may stay on
      // screen. `clear()` is not enough: it empties the cache but leaves mounted observers
      // holding their last result, so the console kept rendering after a sign-out.
      // `resetQueries` resets them and refetches the active ones, which re-asks /auth/me and
      // lands on the sign-in screen.
      void queries.resetQueries();
    },
  });
}
