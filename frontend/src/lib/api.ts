/**
 * REST client for screening decisions.
 *
 * Over HTTP rather than the socket because a decision either worked or did
 * not, and a status code says which. The change still arrives back over the
 * socket, so every open dashboard updates either way.
 */

import type { Call, LineStateResult } from '../types/events';

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
