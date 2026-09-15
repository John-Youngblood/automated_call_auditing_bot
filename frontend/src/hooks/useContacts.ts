/**
 * Naming and starring callers, shared by the live queue and the history view.
 *
 * Two separate flows because they deserve different friction: starring is one
 * click (trivially reversible), naming opens a small dialog (needs typing).
 * Both converge on the same endpoint.
 *
 * Nothing is cached locally. Contact changes come back through the websocket
 * for live calls, and `onChanged` refetches for views that are plain HTTP
 * reads — so there is one source of truth rather than a client-side mirror
 * that can drift.
 */

import { useCallback, useState } from 'react';

import { ApiError, contactsApi } from '../lib/api';

export interface NameTarget {
  number: string;
  /** Formatted for the dialog copy, e.g. "(555) 019-2834". */
  label: string;
  currentName: string;
}

export interface UseContacts {
  /** Number currently being starred/unstarred, for disabling that one button. */
  pendingNumber: string | null;
  toggleFavorite: (number: string, next: boolean) => Promise<void>;

  nameTarget: NameTarget | null;
  namePending: boolean;
  nameError: string | null;
  requestName: (target: NameTarget) => void;
  saveName: (name: string) => Promise<void>;
  clearName: () => Promise<void>;
  cancelName: () => void;

  error: string | null;
  dismissError: () => void;
}

export function useContacts(onChanged?: () => void): UseContacts {
  const [pendingNumber, setPendingNumber] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [nameTarget, setNameTarget] = useState<NameTarget | null>(null);
  const [namePending, setNamePending] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  const describe = (caught: unknown, action: string) =>
    caught instanceof ApiError ? `Could not ${action}: ${caught.message}` : `Could not ${action}.`;

  const toggleFavorite = useCallback(
    async (number: string, next: boolean) => {
      setPendingNumber(number);
      setError(null);
      try {
        // Only isFavorite is sent, so a name saved by someone else survives.
        await contactsApi.save(number, { isFavorite: next });
        onChanged?.();
      } catch (caught) {
        setError(describe(caught, next ? 'add to favourites' : 'remove from favourites'));
      } finally {
        setPendingNumber(null);
      }
    },
    [onChanged],
  );

  const requestName = useCallback((target: NameTarget) => {
    setNameError(null);
    setNameTarget(target);
  }, []);

  const cancelName = useCallback(() => {
    if (namePending) return; // never yank the dialog out from under a live request
    setNameTarget(null);
    setNameError(null);
  }, [namePending]);

  const submit = useCallback(
    async (displayName: string) => {
      if (!nameTarget) return;
      setNamePending(true);
      setNameError(null);
      try {
        await contactsApi.save(nameTarget.number, { displayName });
        setNameTarget(null);
        onChanged?.();
      } catch (caught) {
        // Leave the dialog open so the typed name is not lost.
        setNameError(describe(caught, 'save that name'));
      } finally {
        setNamePending(false);
      }
    },
    [nameTarget, onChanged],
  );

  return {
    pendingNumber,
    toggleFavorite,
    nameTarget,
    namePending,
    nameError,
    requestName,
    saveName: submit,
    // An empty string clears the name server-side; the contact row survives so
    // a star on it is not collateral damage.
    clearName: useCallback(() => submit(''), [submit]),
    cancelName,
    error,
    dismissError: useCallback(() => setError(null), []),
  };
}
