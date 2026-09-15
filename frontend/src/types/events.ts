/**
 * Mirror of the backend websocket protocol.
 *
 * Keep in lockstep with `backend/app/schemas/events.py` and
 * `backend/app/schemas/calls.py`. The backend serialises with camelCase
 * aliases, so field names match one-for-one.
 */

export type CallStatus =
  | 'ringing'
  | 'screening'
  | 'accepted'
  | 'rejected'
  | 'ended'
  /** Terminated because the caller is on the blocklist. */
  | 'blocked';

/** Statuses a call cannot leave. Such calls drop out of the live queue. */
export const TERMINAL_STATUSES: readonly CallStatus[] = [
  'accepted',
  'rejected',
  'ended',
  'blocked',
];

export function isTerminal(status: CallStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export interface Caller {
  number: string | null;
  /** Saved contact name if there is one, else the carrier's caller ID. */
  name: string | null;
  city: string | null;
  country: string | null;
  isFavorite: boolean;
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


/** One finished call, as returned by `GET /api/call-history`. */
export interface CallHistoryEntry {
  callId: string;
  fromNumber: string | null;
  fromName: string | null;
  fromLocation: string | null;
  toNumber: string | null;
  status: CallStatus;
  startedAt: string;
  endedAt: string | null;
  durationSeconds: number | null;
  transcriptSummary: string;
  /** Whether this caller is already on the blocklist. */
  isBlocked: boolean;
  /** Starred right now — resolved server-side at read time. */
  isFavorite: boolean;
}

export interface BlockedNumber {
  number: string;
  originalInput: string | null;
  reason: string | null;
  blockedBy: string | null;
  createdAt: string;
}

/**
 * Result of `POST /api/block-number`.
 *
 * The block and the hang-up are reported separately because they can fail
 * independently: the number goes on the list durably, but dropping a live
 * call is a request to a third party that may not land.
 */
export interface BlockNumberResult {
  blocked: BlockedNumber;
  newlyBlocked: boolean;
  terminatedCallIds: string[];
  failedCallIds: string[];
}

/** A number the team has put its own label on: a name, a star, or both. */
export interface Contact {
  number: string;
  displayName: string | null;
  isFavorite: boolean;
  createdAt: string;
  updatedAt: string;
}
