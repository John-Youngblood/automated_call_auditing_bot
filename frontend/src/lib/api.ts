/**
 * REST client for screening decisions.
 *
 * Over HTTP rather than the socket because a decision either worked or did
 * not, and a status code says which. The change still arrives back over the
 * socket, so every open dashboard updates either way.
 */

import type { Call, LineStateResult } from '../types/events';
import { clearToken, readToken, storeToken } from './auth';

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
    clearToken();
  }

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

export const callsApi = {
  list: () => request<Call[]>('/api/calls'),
  /**
   * Finished calls, newest first. Same `Call` shape as the live queue -- the
   * server holds them in memory, so this list starts empty after a restart.
   */
  history: (limit?: number) =>
    request<Call[]>(`/api/call-history${limit ? `?limit=${limit}` : ''}`),
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
