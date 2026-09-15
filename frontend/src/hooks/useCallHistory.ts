/**
 * Finished calls, read over HTTP.
 *
 * History is a plain paged read rather than a websocket stream: it changes
 * only when a call ends, and streaming a growing list to every dashboard
 * would cost more than refetching a page on the rare occasions it changes.
 *
 * `refreshKey` is how live events reach it -- the dashboard increments it on
 * `call.ended`, so a call that just finished shows up without a manual reload.
 */

import { useCallback, useEffect, useState } from 'react';

import { moderationApi } from '../lib/api';
import type { CallHistoryEntry } from '../types/events';

export interface UseCallHistory {
  entries: CallHistoryEntry[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useCallHistory(refreshKey: number, enabled = true): UseCallHistory {
  const [entries, setEntries] = useState<CallHistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manualKey, setManualKey] = useState(0);

  useEffect(() => {
    if (!enabled) return;

    // Guards against a slow response from a previous key overwriting a newer
    // one, and against setting state after unmount.
    let current = true;
    setLoading(true);

    moderationApi
      .history()
      .then((rows) => {
        if (!current) return;
        setEntries(rows);
        setError(null);
      })
      .catch(() => {
        if (current) setError('Could not load call history.');
      })
      .finally(() => {
        if (current) setLoading(false);
      });

    return () => {
      current = false;
    };
  }, [refreshKey, manualKey, enabled]);

  return {
    entries,
    loading,
    error,
    refresh: useCallback(() => setManualKey((n) => n + 1), []),
  };
}
