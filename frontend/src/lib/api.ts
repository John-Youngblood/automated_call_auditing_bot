/**
 * REST client for screening decisions.
 *
 * Decisions go over HTTP rather than the websocket on purpose: they are
 * one-shot actions where the caller needs to know whether it worked, and a
 * status code gives that directly. The websocket stays a one-way stream of
 * state. The resulting change still arrives back through the socket, so every
 * open dashboard updates either way.
 */

import type {
  BlockedNumber,
  BlockNumberResult,
  Call,
  CallHistoryEntry,
  Contact,
} from '../types/events';

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });

  if (!response.ok) {
    // FastAPI puts the reason in `detail`; fall back to the status text.
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body.detail)
      .catch(() => undefined);
    throw new ApiError(detail ?? response.statusText, response.status);
  }

  return (await response.json()) as T;
}

export const contactsApi = {
  /**
   * Create or update a contact. Omitted fields are left alone, so the star
   * button can toggle a favourite without disturbing a name and vice versa —
   * send an empty string to actually clear a name.
   */
  save: (number: string, changes: { displayName?: string; isFavorite?: boolean }) =>
    request<Contact>('/api/contacts', {
      method: 'POST',
      body: JSON.stringify({ number, ...changes }),
    }),
  list: () => request<Contact[]>('/api/contacts'),
  remove: (number: string) =>
    request<{ status: string }>(`/api/contacts/${encodeURIComponent(number)}`, {
      method: 'DELETE',
    }),
};

export const moderationApi = {
  /**
   * Block a caller. The server normalises the number, so the UI can pass
   * whatever it has -- the provider's E.164 string or something a moderator
   * typed -- without having to agree on a format first.
   */
  blockNumber: (number: string, options: { reason?: string; blockedBy?: string } = {}) =>
    request<BlockNumberResult>('/api/block-number', {
      method: 'POST',
      body: JSON.stringify({ number, ...options }),
    }),
  listBlocked: () => request<BlockedNumber[]>('/api/blocked-numbers'),
  history: (limit?: number) =>
    request<CallHistoryEntry[]>(`/api/call-history${limit ? `?limit=${limit}` : ''}`),
};

export const callsApi = {
  list: () => request<Call[]>('/api/calls'),
  get: (callId: string) => request<Call>(`/api/calls/${encodeURIComponent(callId)}`),
  accept: (callId: string) =>
    request<Call>(`/api/calls/${encodeURIComponent(callId)}/accept`, { method: 'POST' }),
  reject: (callId: string) =>
    request<Call>(`/api/calls/${encodeURIComponent(callId)}/reject`, { method: 'POST' }),
};
