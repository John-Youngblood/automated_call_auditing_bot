import { useCallback, useEffect, useState } from 'react';

import { ApiError, callsApi } from '../lib/api';
import type { Call } from '../types/events';

interface CallHistory {
  calls: Call[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/**
 * Finished calls for the history view.
 *
 * Fetched rather than derived from the websocket: the socket only carries what
 * happened while this tab was connected, so a dashboard opened mid-show would
 * otherwise show a history that starts when the browser did.
 *
 * `endedCount` is the refetch trigger -- it ticks every time a call reaches a
 * terminal status, so the list refreshes itself when a call finishes instead of
 * polling on a timer. `enabled` keeps it quiet while the view is closed.
 */
export function useCallHistory(endedCount: number, enabled: boolean): CallHistory {
  const [calls, setCalls] = useState<Call[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) return;

    // Guards against a slow response landing after a newer one, which would
    // show stale rows, and against setting state on an unmounted component.
    let cancelled = false;
    setLoading(true);

    callsApi
      .history()
      .then((rows) => {
        if (cancelled) return;
        setCalls(rows);
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
  }, [enabled, endedCount, nonce]);

  return { calls, loading, error, refresh };
}
