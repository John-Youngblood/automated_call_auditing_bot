/**
 * REST client for screening decisions, call history and contacts.
 *
 * Over HTTP rather than the socket because a decision either worked or did
 * not, and a status code says which. The change still arrives back over the
 * socket, so every open dashboard updates either way.
 */

import type {
  Call,
  CallHistoryPage,
  ContactChangeResult,
  ContactListItem,
  ContactPatch,
  LineStateResult,
} from '../types/events';
import { clearToken, expireSession, readToken, storeToken } from './auth';

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  /** The token is missing, wrong, or died with a backend restart. */
  get needsSignIn(): boolean {
    return this.status === 401;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = readToken();
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });

  if (response.status === 401) {
    // Drop it rather than keep retrying with a token the server has forgotten.
    expireSession();
  }

  if (!response.ok) {
    // FastAPI puts the reason in `detail`; fall back to the status text. A
    // validation error (422) puts a list there instead, which reads as
    // "[object Object]" if passed through.
    const detail = await response
      .json()
      .then((body: { detail?: unknown }) =>
        typeof body.detail === 'string' ? body.detail : undefined,
      )
      .catch(() => undefined);
    throw new ApiError(detail ?? response.statusText, response.status);
  }

  return (await response.json()) as T;
}

export interface HistoryQuery {
  /** `next` from the previous page. */
  before?: string;
  /** Only calls from this number. */
  number?: string;
  /** Matches what callers said, contact names and notes, and number digits. */
  q?: string;
  limit?: number;
}

export const callsApi = {
  list: () => request<Call[]>('/api/calls'),
  /**
   * Finished calls, newest first, one page at a time. Same `Call` shape as the
   * live queue. Kept on disk, so they outlive a backend restart.
   */
  history: ({ before, number, q, limit }: HistoryQuery = {}) => {
    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (before) params.set('before', before);
    if (number) params.set('number', number);
    if (q) params.set('q', q);
    const query = params.toString();
    return request<CallHistoryPage>(`/api/call-history${query ? `?${query}` : ''}`);
  },
  get: (callId: string) => request<Call>(`/api/calls/${encodeURIComponent(callId)}`),
  accept: (callId: string) =>
    request<Call>(`/api/calls/${encodeURIComponent(callId)}/accept`, { method: 'POST' }),
  reject: (callId: string) =>
    request<Call>(`/api/calls/${encodeURIComponent(callId)}/reject`, { method: 'POST' }),
  /**
   * Go on or off air. Closing turns new callers away *and* hangs up on anyone
   * still holding, so it can take a moment while those calls are ended.
   */
  setLineOpen: (open: boolean) =>
    request<LineStateResult>(`/api/line/${open ? 'open' : 'close'}`, { method: 'POST' }),
};

export const contactsApi = {
  list: () => request<ContactListItem[]>('/api/contacts'),
  /**
   * Create, edit or remove the contact for a number. The server accepts it
   * typed any common way and answers with the canonical form. Blocking also
   * hangs up on that number's open calls, so this can take a moment.
   */
  update: (number: string, patch: ContactPatch) =>
    request<ContactChangeResult>(`/api/contacts/${encodeURIComponent(number)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
};

export const sessionApi = {
  /** Exchange the dashboard password for a token, and remember it. */
  async logIn(password: string): Promise<void> {
    const { token } = await request<{ token: string }>('/api/session', {
      method: 'POST',
      body: JSON.stringify({ password }),
    });
    storeToken(token);
  },

  async logOut(): Promise<void> {
    // Best effort: the token is dropped locally whatever the server says.
    try {
      await request<void>('/api/session', { method: 'DELETE' });
    } finally {
      clearToken();
    }
  },
};
