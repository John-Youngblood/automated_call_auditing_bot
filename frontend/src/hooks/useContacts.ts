import { useEffect, useMemo, useState } from 'react';

import { ApiError, contactsApi } from '../lib/api';
import type { ContactListItem, ContactStanding } from '../types/events';

export type ContactFilter = 'all' | ContactStanding;

export interface Contacts {
  /** After the filter and search. */
  contacts: ContactListItem[];
  /** Per filter, before the search, for the chip labels. */
  counts: Record<ContactFilter, number>;
  /** Nothing saved at all, as opposed to nothing matching. */
  empty: boolean;
  loading: boolean;
  error: string | null;
  query: string;
  setQuery: (query: string) => void;
  filter: ContactFilter;
  setFilter: (filter: ContactFilter) => void;
}

/**
 * The contacts list, fetched while its tab is open and again whenever any
 * dashboard changes a contact.
 *
 * Searched and filtered here rather than on the server: it is one row per
 * number someone bothered to name, note or block, so it stays small.
 */
export function useContacts(contactsVersion: number, enabled: boolean): Contacts {
  const [all, setAll] = useState<ContactListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ContactFilter>('all');

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    setLoading(true);

    contactsApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setAll(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : 'Could not load contacts.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, contactsVersion]);

  const counts = useMemo(
    () => ({
      all: all.length,
      favorite: all.filter((c) => c.standing === 'favorite').length,
      blocked: all.filter((c) => c.standing === 'blocked').length,
    }),
    [all],
  );

  const contacts = useMemo(() => {
    const text = query.trim().toLowerCase();
    // Digits match the number however it was typed: "(503) 555" finds
    // +15035550147. Fewer than three would match nearly everyone.
    const digits = text.replace(/\D/g, '');
    return all.filter(
      (c) =>
        (filter === 'all' || c.standing === filter) &&
        (!text ||
          c.name?.toLowerCase().includes(text) ||
          c.note?.toLowerCase().includes(text) ||
          (digits.length >= 3 && c.number.includes(digits))),
    );
  }, [all, filter, query]);

  return {
    contacts,
    counts,
    empty: all.length === 0,
    loading,
    error,
    query,
    setQuery,
    filter,
    setFilter,
  };
}
