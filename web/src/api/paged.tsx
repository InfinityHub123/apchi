import { useInfiniteQuery } from "@tanstack/react-query";

/**
 * A cursor-paged listing with "Load older".
 *
 * Every listing in the API pages the same way -- `items`, and a `next` to pass back as
 * `before` -- because offsets drift when something is inserted at the top, and the top is
 * exactly where Applies, Validations and audit events arrive.
 */
export function usePaged<T>(
  key: unknown[],
  fetchPage: (before: string | undefined) => Promise<{ items: T[]; next?: string | null }>,
  options: { refetchInterval?: number | false } = {},
) {
  const query = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam }) => fetchPage(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next ?? undefined,
    refetchInterval: options.refetchInterval,
  });
  return {
    items: query.data?.pages.flatMap((p) => p.items) ?? [],
    loaded: query.data !== undefined,
    error: query.error,
    hasMore: query.hasNextPage,
    loadMore: () => void query.fetchNextPage(),
    loadingMore: query.isFetchingNextPage,
    refetch: query.refetch,
  };
}

export function LoadOlder({ paged }: { paged: { hasMore: boolean; loadMore: () => void; loadingMore: boolean } }) {
  if (!paged.hasMore) return null;
  return (
    <div className="cft">
      <button type="button" className="btn" onClick={paged.loadMore} disabled={paged.loadingMore}>
        {paged.loadingMore ? "Loading…" : "Load older"}
      </button>
    </div>
  );
}
