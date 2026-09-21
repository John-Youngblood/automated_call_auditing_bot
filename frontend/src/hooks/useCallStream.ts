/**
 * Owns the dashboard's live state: one websocket, one reducer, one place where
 * server events turn into UI state.
 *
 * Components stay presentational and receive plain data. That split is what
 * makes the queue and transcript testable without a socket, and keeps
 * reconnection logic out of the render path.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import { ApiError, callsApi } from '../lib/api';
import { ReconnectingSocket, resolveWebSocketUrl } from '../lib/websocket';
import type { Call, ConnectionStatus, ServerEvent } from '../types/events';
import { isServerEvent, isTerminal } from '../types/events';

/** Finished calls kept for review before being pruned. */
const MAX_TERMINAL_CALLS = 20;

interface State {
  connection: ConnectionStatus;
  calls: Record<string, Call>;
  /** Call ids in arrival order; the queue renders from this. */
  order: string[];
  lastError: string | null;
  /**
   * Bumped every time a call finishes. The history view is a plain HTTP read,
   * so this is the signal that tells it something new is worth fetching.
   */
  endedCount: number;
  /** Whether the show is taking new calls. */
  lineOpen: boolean;
}

const initialState: State = {
  connection: 'connecting',
  calls: {},
  order: [],
  lastError: null,
  endedCount: 0,
  // Assumed open until the first snapshot says otherwise, which arrives
  // immediately on connect. The alternative -- assuming closed -- would flash
  // "off air" on every page load during a live show.
  lineOpen: true,
};

type Action =
  | { kind: 'connection'; status: ConnectionStatus }
  | { kind: 'event'; event: ServerEvent }
  | { kind: 'error'; message: string | null };

function upsertCall(state: State, call: Call): State {
  return {
    ...state,
    calls: { ...state.calls, [call.callId]: call },
    order: state.order.includes(call.callId) ? state.order : [...state.order, call.callId],
  };
}

function pruneTerminal(state: State): State {
  const terminal = state.order.filter((id) => {
    const call = state.calls[id];
    return call !== undefined && isTerminal(call.status);
  });
  if (terminal.length <= MAX_TERMINAL_CALLS) return state;

  const drop = new Set(terminal.slice(0, terminal.length - MAX_TERMINAL_CALLS));
  const calls = { ...state.calls };
  for (const id of drop) delete calls[id];
  return { ...state, calls, order: state.order.filter((id) => !drop.has(id)) };
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case 'connection':
      return { ...state, connection: action.status };

    case 'error':
      return { ...state, lastError: action.message };

    case 'event': {
      const { event } = action;
      switch (event.type) {
        case 'state.snapshot': {
          // Authoritative replacement: sent on every (re)connect, so it is
          // also how the dashboard recovers state after a dropped socket.
          const calls: Record<string, Call> = {};
          for (const call of event.data.calls) calls[call.callId] = call;
          return {
            ...state,
            calls,
            order: event.data.calls.map((call) => call.callId),
            lineOpen: event.data.lineOpen,
          };
        }

        case 'line.changed':
          // Another operator (or this one) opened or closed the line. Every
          // dashboard follows, so two people cannot disagree about whether
          // the show is taking calls.
          return { ...state, lineOpen: event.data.lineOpen };

        case 'call.incoming':
        case 'call.updated':
          return upsertCall(state, event.data.call);

        case 'call.ended': {
          const next = pruneTerminal(upsertCall(state, event.data.call));
          return { ...next, endedCount: next.endedCount + 1 };
        }

        case 'error':
          return { ...state, lastError: event.data.message };

        case 'pong':
          return state;

        default: {
          // Exhaustiveness guard: a new server event type fails to compile
          // here instead of being silently dropped at runtime.
          const unhandled: never = event;
          console.warn('[ws] unhandled event', unhandled);
          return state;
        }
      }
    }
  }
}

export interface UseCallStream {
  connection: ConnectionStatus;
  /** Increments when a call finishes; drives the history view's refetch. */
  endedCount: number;
  lastError: string | null;
  /** Whether the show is taking new calls. */
  lineOpen: boolean;
  /** Calls awaiting a decision or being screened, oldest first. */
  activeCalls: Call[];
  /** Recently finished calls, newest first. */
  recentCalls: Call[];
  selectedCall: Call | null;
  selectCall: (callId: string) => void;
  acceptCall: (callId: string) => Promise<void>;
  rejectCall: (callId: string) => Promise<void>;
  pendingCallId: string | null;
  dismissError: () => void;
}

export function useCallStream(): UseCallStream {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [manualSelection, setManualSelection] = useState<string | null>(null);
  const [pendingCallId, setPendingCallId] = useState<string | null>(null);
  const socketRef = useRef<ReconnectingSocket | null>(null);

  useEffect(() => {
    const socket = new ReconnectingSocket({
      url: resolveWebSocketUrl('/ws/frontend'),
      onMessage: (data) => {
        if (isServerEvent(data)) dispatch({ kind: 'event', event: data });
      },
      onStatusChange: (status) => dispatch({ kind: 'connection', status }),
    });
    socketRef.current = socket;
    socket.connect();

    // Runs on unmount and on StrictMode's dev double-mount; without it the
    // first socket would keep reconnecting invisibly forever.
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, []);

  const activeCalls = useMemo(
    () =>
      state.order
        .map((id) => state.calls[id])
        .filter((call): call is Call => call !== undefined && !isTerminal(call.status)),
    [state.order, state.calls],
  );

  const recentCalls = useMemo(
    () =>
      state.order
        .map((id) => state.calls[id])
        .filter((call): call is Call => call !== undefined && isTerminal(call.status))
        .reverse(),
    [state.order, state.calls],
  );

  // Auto-follow the oldest ringing call unless the operator picked one. Keeps
  // the pane useful hands-off, without yanking focus off a call being read.
  const selectedCall = useMemo(() => {
    if (manualSelection) {
      const chosen = state.calls[manualSelection];
      if (chosen) return chosen;
    }
    return activeCalls[0] ?? recentCalls[0] ?? null;
  }, [manualSelection, state.calls, activeCalls, recentCalls]);

  const decide = useCallback(
    async (callId: string, action: 'accept' | 'reject') => {
      setPendingCallId(callId);
      dispatch({ kind: 'error', message: null });
      try {
        // The optimistic update is deliberately omitted: the backend
        // broadcasts the new status, so every dashboard converges on the same
        // state from one source instead of two.
        await (action === 'accept' ? callsApi.accept(callId) : callsApi.reject(callId));
      } catch (error) {
        const message =
          error instanceof ApiError
            ? `Could not ${action} call: ${error.message}`
            : `Could not ${action} call: network error`;
        dispatch({ kind: 'error', message });
      } finally {
        setPendingCallId(null);
      }
    },
    [],
  );

  return {
    connection: state.connection,
    endedCount: state.endedCount,
    lastError: state.lastError,
    lineOpen: state.lineOpen,
    activeCalls,
    recentCalls,
    selectedCall,
    selectCall: setManualSelection,
    acceptCall: useCallback((callId: string) => decide(callId, 'accept'), [decide]),
    rejectCall: useCallback((callId: string) => decide(callId, 'reject'), [decide]),
    pendingCallId,
    dismissError: useCallback(() => dispatch({ kind: 'error', message: null }), []),
  };
}
