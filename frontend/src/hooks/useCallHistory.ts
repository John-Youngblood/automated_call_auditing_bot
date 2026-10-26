import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, callsApi } from '../lib/api';
import type { Call, CallHistoryPage } from '../types/events';

/** Rows per request; the server allows up to `MAX_PAGE_SIZE`. */
const PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;

/** Long enough that typing a word is one request, not one per letter. */
const SEARCH_DEBOUNCE_MS = 300;

export interface CallHistory {
  calls: Call[];
  loading: boolean;
  loadingOlder: boolean;
  error: string | null;
  /** The server has calls older than the last one shown. */
  hasMore: boolean;
  /** What is in the search box, ahead of the debounced search itself. */
  query: string;
  setQuery: (query: string) => void;
  /** Only calls from this number, or null for everyone. */
  number: string | null;
  /** Filter to one number, clearing the search so the two cannot fight. */
  showNumber: (number: string | null) => void;
  refresh: () => void;
  loadOlder: () => void;
}

interface Loaded {
  /** The filters these rows answer, so a stale response cannot land on new ones. */
  filters: string;
  calls: Call[];
  next: string | null;
}

/**
 * Finished calls for the history view, a page at a time.
 *
 * Fetched rather than derived from the websocket: the socket only carries what
 * happened while this tab was connected, so a dashboard opened mid-show would
 * otherwise show a history that starts when the browser did.
 *
 * `endedCount` and `contactsVersion` are the refetch triggers -- one ticks when
 * a call finishes, the other when a name, note or block changes -- so the list
 * refreshes itself instead of polling on a timer. `enabled` keeps it quiet
 * while the view is closed.
 *
 * A refetch asks for as many rows as are already on screen (up to the server's
 * cap) and folds them in by position, so an operator reading older calls is
 * not yanked back to the top, and the rows they are reading still pick up a
 * contact change.
 */
export function useCallHistory(
  endedCount: number,
  contactsVersion: number,
  enabled: boolean,
): CallHistory {
  const [query, setQuery] = useState('');
  const [q, setQ] = useState('');
  const [number, setNumber] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Loaded>({ filters: '', calls: [], next: null });
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const filters = JSON.stringify([number, q]);

  // Read by the fetch below without making the row count a refetch trigger.
  const heldRef = useRef(0);
  useEffect(() => {
    heldRef.current = loaded.filters === filters ? loaded.calls.length : 0;
  }, [loaded, filters]);

  useEffect(() => {
    const timer = window.setTimeout(() => setQ(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  const showNumber = useCallback((next: string | null) => {
    setNumber(next);
    setQuery('');
    setQ('');
  }, []);

  useEffect(() => {
    if (!enabled) return;

    // Guards against a slow response landing after a newer one, which would
    // show stale rows, and against setting state on an unmounted component.
    let cancelled = false;
    setLoading(true);

    callsApi
      .history({
        number: number ?? undefined,
        q: q || undefined,
        limit: Math.min(MAX_PAGE_SIZE, Math.max(PAGE_SIZE, heldRef.current)),
      })
      .then((page) => {
        if (cancelled) return;
        setLoaded((current) =>
          mergeFirstPage(current.filters === filters ? current : null, page, filters),
        );
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : 'Could not load call history.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, endedCount, contactsVersion, nonce, number, q]);

  const loadOlder = useCallback(() => {
    const cursor = loaded.next;
    if (!cursor || loadingOlder || loaded.filters !== filters) return;
    setLoadingOlder(true);

    callsApi
      .history({ before: cursor, number: number ?? undefined, q: q || undefined, limit: PAGE_SIZE })
      .then((page) => {
        setLoaded((current) =>
          // A refetch replaced the list meanwhile, so this page no longer
          // follows on from it.
          current.filters !== filters || current.next !== cursor
            ? current
            : { ...current, calls: [...current.calls, ...page.calls], next: page.next },
        );
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : 'Could not load older calls.');
      })
      .finally(() => setLoadingOlder(false));
  }, [loaded, loadingOlder, filters, number, q]);

  return {
    calls: loaded.calls,
    loading,
    loadingOlder,
    error,
    hasMore: loaded.filters === filters && loaded.next !== null,
    query,
    setQuery,
    number,
    showNumber,
    refresh,
    loadOlder,
  };
}

/**
 * Fold a fresh first page into the rows already loaded for the same filters.
 *
 * Rows older than the fresh page's last one are ones the operator paged to, so
 * they stay, along with the cursor that continues after them. Anything newer
 * is answered by the fresh page alone, which also drops rows that no longer
 * match the search.
 */
function mergeFirstPage(held: Loaded | null, page: CallHistoryPage, filters: string): Loaded {
  const last = page.calls.at(-1);
  const older =
    held && last && page.next !== null ? held.calls.filter((c) => sortsAfter(c, last)) : [];
  return older.length > 0 && held
    ? { filters, calls: [...page.calls, ...older], next: held.next }
    : { filters, calls: page.calls, next: page.next };
}

/** The server's order: newest first, call id breaking ties. */
function sortsAfter(call: Call, other: Call): boolean {
  const a = Date.parse(call.startedAt);
  const b = Date.parse(other.startedAt);
  return a < b || (a === b && call.callId < other.callId);
}
