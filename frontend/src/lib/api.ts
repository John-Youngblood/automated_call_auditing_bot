/**
 * REST client for screening decisions.
 *
 * Decisions go over HTTP rather than the websocket on purpose: they are
 * one-shot actions where the caller needs to know whether it worked, and a
 * status code gives that directly. The websocket stays a one-way stream of
 * state. The resulting change still arrives back through the socket, so every
 * open dashboard updates either way.
 */

import type { Call } from '../types/events';

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
};
