/**
 * The block-a-caller flow, shared by the live queue and the history view.
 *
 * Both entry points need the same three things -- a confirmation step, a
 * pending state, and an error that survives long enough to be read -- so the
 * flow lives here once rather than being reimplemented per call site.
 *
 * Nothing blocks anyone until `confirm()` is called: `request()` only opens
 * the dialog.
 */

import { useCallback, useState } from 'react';

import { ApiError, moderationApi } from '../lib/api';
import type { BlockNumberResult } from '../types/events';

export interface BlockTarget {
  /** Raw number to send; the server normalises it. */
  number: string;
  /** Formatted for the confirmation copy, e.g. "(555) 019-2834". */
  label: string;
  /** Present when the block was triggered from a live call. */
  callId?: string;
  /** Starred callers get an extra warning before being blocked. */
  isFavorite?: boolean;
}

export interface UseBlockNumber {
  target: BlockTarget | null;
  pending: boolean;
  error: string | null;
  /** Open the confirmation dialog for this caller. */
  request: (target: BlockTarget) => void;
  confirm: () => Promise<void>;
  cancel: () => void;
  /** Set after a successful block, for a short confirmation message. */
  lastResult: BlockNumberResult | null;
  clearResult: () => void;
}

export function useBlockNumber(onBlocked?: () => void): UseBlockNumber {
  const [target, setTarget] = useState<BlockTarget | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<BlockNumberResult | null>(null);

  const request = useCallback((next: BlockTarget) => {
    setError(null);
    setTarget(next);
  }, []);

  const cancel = useCallback(() => {
    if (pending) return; // never yank the dialog out from under a live request
    setTarget(null);
    setError(null);
  }, [pending]);

  const confirm = useCallback(async () => {
    if (!target) return;
    setPending(true);
    setError(null);
    try {
      const result = await moderationApi.blockNumber(target.number, {
        reason: target.callId ? 'blocked from live queue' : 'blocked from call history',
      });
      setLastResult(result);
      setTarget(null);
      // The queue updates itself over the websocket; this is for views that
      // are plain HTTP reads, like call history.
      onBlocked?.();
    } catch (caught) {
      // Keep the dialog open on failure. Closing it would leave the moderator
      // unsure whether the caller was actually blocked.
      setError(
        caught instanceof ApiError
          ? `Could not block ${target.label}: ${caught.message}`
          : `Could not block ${target.label}: network error`,
      );
    } finally {
      setPending(false);
    }
  }, [target, onBlocked]);

  return {
    target,
    pending,
    error,
    request,
    confirm,
    cancel,
    lastResult,
    clearResult: useCallback(() => setLastResult(null), []),
  };
}
