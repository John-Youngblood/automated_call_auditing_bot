/**
 * Mirror of the backend websocket protocol.
 *
 * Keep in lockstep with `backend/app/schemas/events.py` and
 * `backend/app/schemas/calls.py`. The backend serialises with camelCase
 * aliases, so field names match one-for-one.
 */

export type CallStatus =
  /** Caller is saying why they are calling; Twilio is still listening. */
  | 'screening'
  /** Transcript is in; the call is on hold awaiting an operator decision. */
  | 'on-hold'
  | 'accepted'
  | 'rejected'
  /** The caller hung up, or the provider reported the call over. */
  | 'ended';

/**
 * Statuses a call cannot leave. Such calls drop out of the live queue and into
 * Call History.
 */
export const TERMINAL_STATUSES: readonly CallStatus[] = ['accepted', 'rejected', 'ended'];

export function isTerminal(status: CallStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export interface Caller {
  number: string | null;
  /**
   * The carrier's caller-ID name, when Twilio sends one. Usually absent, and
   * often generic ("WIRELESS CALLER") when present.
   */
  name: string | null;
  /**
   * Where the *number* is registered, e.g. "Portland, OR" — already formatted
   * by the backend, so the state-vs-country rule exists in one language only.
   * Not the caller's actual location: a ported mobile keeps its old area code.
   */
  location: string | null;
}

export interface Call {
  callId: string;
  status: CallStatus;
  caller: Caller;
  toNumber: string | null;
  startedAt: string;
  endedAt: string | null;
  /**
   * What the caller said when asked why they are calling. Arrives complete in
   * one update once they stop speaking, so there is no partial state to
   * reconcile -- it is either null or the whole thing.
   */
  transcript: string | null;
  /** Twilio's confidence in that transcription, 0-1. */
  transcriptConfidence: number | null;
  /**
   * True when the backend rebuilt this call from Twilio after a restart. The
   * caller is really on hold, but their transcript died with the previous
   * process — so the UI must say so rather than render the same empty state
   * as a caller who genuinely said nothing.
   */
  recovered: boolean;
}

export type ServerEventType =
  | 'state.snapshot'
  | 'call.incoming'
  | 'call.updated'
  | 'call.ended'
  | 'pong'
  | 'error';

interface BaseServerEvent {
  ts: string;
  callId: string | null;
}

/**
 * Discriminated union on `type`, so narrowing in the reducer is exhaustive and
 * a new backend event type becomes a compile error rather than silent
 * no-op at runtime.
 */
export type ServerEvent =
  | (BaseServerEvent & { type: 'state.snapshot'; data: { calls: Call[] } })
  | (BaseServerEvent & { type: 'call.incoming'; data: { call: Call } })
  | (BaseServerEvent & { type: 'call.updated'; data: { call: Call } })
  | (BaseServerEvent & { type: 'call.ended'; data: { call: Call } })
  | (BaseServerEvent & { type: 'pong'; data: Record<string, never> })
  | (BaseServerEvent & { type: 'error'; data: { message: string } });

export type ClientCommand =
  | { type: 'ping' }
  | { type: 'call.accept'; callId: string }
  | { type: 'call.reject'; callId: string };

export type ConnectionStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

/** Narrowing guard for untrusted socket payloads. */
export function isServerEvent(value: unknown): value is ServerEvent {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { type?: unknown }).type === 'string'
  );
}

