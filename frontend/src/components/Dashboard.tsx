import { useState } from 'react';

import { sessionApi } from '../lib/api';

import { useCallHistory } from '../hooks/useCallHistory';
import { useToasts } from '../hooks/useToasts';
import { useCallStream } from '../hooks/useCallStream';
import { useNow } from '../hooks/useNow';
import { formatClock, formatElapsed, formatPhoneNumber, statusLabel } from '../lib/format';
import CallActions from './CallActions';
import LineControls from './LineControls';
import Toasts from './Toasts';
import CallHistoryView from './CallHistoryView';
import CallQueue from './CallQueue';
import ConnectionBadge from './ConnectionBadge';
import TranscriptPanel from './TranscriptPanel';

type View = 'live' | 'history';

interface Props {
  onSignedOut: () => void;
}

/**
 * Dashboard shell. All state comes from hooks; this component arranges it.
 *
 * Two top-level views rather than one crowded screen: the live queue is a work
 * surface where seconds matter, history is a record you read at leisure. The
 * queue keeps running in the background either way -- switching to history
 * does not disconnect the websocket, so nothing is missed.
 */
export default function Dashboard({ onSignedOut }: Props) {
  const [view, setView] = useState<View>('live');

  const {
    connection,
    lastError,
    activeCalls,
    recentCalls,
    selectedCall,
    selectCall,
    acceptCall,
    rejectCall,
    pendingCallId,
    dismissError,
    endedCount,
    lineOpen,
    screeningNumber,
  } = useCallStream();

  const now = useNow();

  // Only fetch history while that view is open; the queue is the hot path.
  const history = useCallHistory(endedCount, view === 'history');

  const { toasts, push, dismiss } = useToasts();

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          {/* Named rather than decorative: the logo is the only place the show
              itself is identified in the UI, so alt="" would drop that. */}
          <img className="app__logo" src="/h3_logo.png" alt="The H3 Podcast" />
          <h1>Call Screener</h1>

          {/* Beside the wordmark because it is the one thing an operator reads
              off the screen to someone else, mid-show. Hidden entirely when
              TWILIO_PHONE_NUMBER is unset, rather than rendering an empty
              label somebody might read out by mistake. */}
          {screeningNumber && (
            <p className="callin">
              <span className="callin__label">Call in</span>
              <span className="callin__number">{formatPhoneNumber(screeningNumber)}</span>
            </p>
          )}
        </div>

        <nav className="tabs" aria-label="Views">
          <button
            type="button"
            className={`tab${view === 'live' ? ' tab--active' : ''}`}
            onClick={() => setView('live')}
            aria-current={view === 'live'}
          >
            Live Queue
            {activeCalls.length > 0 && <span className="tab__count">{activeCalls.length}</span>}
          </button>
          <button
            type="button"
            className={`tab${view === 'history' ? ' tab--active' : ''}`}
            onClick={() => setView('history')}
            aria-current={view === 'history'}
          >
            Call History
          </button>
        </nav>

        <div className="app__header-right">
          <ConnectionBadge status={connection} />
          <button
            type="button"
            className="app__signout"
            onClick={() => {
              void sessionApi.logOut().finally(onSignedOut);
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      {lastError && (
        <div className="alert" role="alert">
          <span>{lastError}</span>
          <button type="button" className="alert__dismiss" onClick={dismissError}>
            Dismiss
          </button>
        </div>
      )}


      {view === 'live' ? (
        <main className="app__body">
          <CallQueue
            activeCalls={activeCalls}
            recentCalls={recentCalls}
            selectedCallId={selectedCall?.callId ?? null}
            onSelect={selectCall}
            now={now}
          />

          <section className="detail" aria-label="Active call">
            {!selectedCall ? (
              <div className="detail__placeholder">
                <h2>Waiting for calls</h2>
                <p>
                  {lineOpen
                    ? 'Incoming calls appear here once the caller has said why they’re calling.'
                    : 'The line is closed — new callers are being turned away.'}
                  {connection !== 'open' && ' Reconnecting to the call stream…'}
                </p>
              </div>
            ) : (
              <>
                <header className="detail__header">
                  <div>
                    <h2>
                      {selectedCall.caller.name ??
                        formatPhoneNumber(selectedCall.caller.number)}
                    </h2>
                    <p className="detail__subtitle">
                      {[
                        // Name is the headline, so the number belongs here.
                        selectedCall.caller.name
                          ? formatPhoneNumber(selectedCall.caller.number)
                          : null,
                        selectedCall.caller.location,
                        `started ${formatClock(selectedCall.startedAt)}`,
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </p>
                  </div>
                  <div className="detail__timing">
                    <span className={`status status--${selectedCall.status}`}>
                      {statusLabel(selectedCall.status)}
                    </span>
                    <span className="detail__elapsed">
                      {formatElapsed(
                        selectedCall.startedAt,
                        selectedCall.endedAt ? new Date(selectedCall.endedAt).getTime() : now,
                      )}
                    </span>
                  </div>
                </header>

                <TranscriptPanel call={selectedCall} />

                <CallActions
                  call={selectedCall}
                  pending={pendingCallId === selectedCall.callId}
                  onAccept={acceptCall}
                  onReject={rejectCall}
                />
              </>
            )}
          </section>
        </main>
      ) : (
        <main className="app__body app__body--single">
          <CallHistoryView
            calls={history.calls}
            loading={history.loading}
            error={history.error}
            onRefresh={history.refresh}
          />
        </main>
      )}

      {/* Below the work surface on purpose. Going off air is a once-a-show
          decision, not something an operator reaches for between calls, and
          it does not belong next to the tabs they use constantly. */}
      <footer className="app__footer">
        <LineControls open={lineOpen} onNotice={push} />

        {/* Baked in at build time from package.json, so it identifies the
            bundle this browser is actually running -- not what the server
            most recently deployed. That difference is the whole point when
            someone reports a bug from a tab they left open yesterday. */}
        <span className="app__version">v{import.meta.env.VITE_APP_VERSION}</span>
      </footer>

      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}

