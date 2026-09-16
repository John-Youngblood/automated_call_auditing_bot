import { useCallback, useState } from 'react';

import { useBlockNumber } from '../hooks/useBlockNumber';
import { useContacts } from '../hooks/useContacts';
import { useCallHistory } from '../hooks/useCallHistory';
import { useCallStream } from '../hooks/useCallStream';
import { useNow } from '../hooks/useNow';
import { formatClock, formatElapsed, formatPhoneNumber, statusLabel } from '../lib/format';
import type { Call, CallHistoryEntry } from '../types/events';
import CallActions from './CallActions';
import CallHistoryView from './CallHistoryView';
import CallQueue from './CallQueue';
import ConfirmDialog from './ConfirmDialog';
import ConnectionBadge from './ConnectionBadge';
import FavoriteStar from './FavoriteStar';
import NameContactDialog from './NameContactDialog';
import TranscriptPanel from './TranscriptPanel';

type View = 'live' | 'history';

/**
 * Dashboard shell. All state comes from hooks; this component arranges it.
 *
 * Two top-level views rather than one crowded screen: the live queue is a work
 * surface where seconds matter, history is a record you read at leisure. The
 * queue keeps running in the background either way -- switching to history
 * does not disconnect the websocket, so nothing is missed.
 */
export default function Dashboard() {
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
  } = useCallStream();

  const now = useNow();

  // Only fetch history while that view is open; the queue is the hot path.
  const history = useCallHistory(endedCount, view === 'history');

  const block = useBlockNumber(history.refresh);
  const contacts = useContacts(history.refresh);

  const requestBlockFromCall = useCallback(
    (call: Call) => {
      if (!call.caller.number) return;
      block.request({
        number: call.caller.number,
        label: call.caller.name ?? formatPhoneNumber(call.caller.number),
        callId: call.callId,
        isFavorite: call.caller.isFavorite,
      });
    },
    [block],
  );

  const requestBlockFromHistory = useCallback(
    (entry: CallHistoryEntry) => {
      if (!entry.fromNumber) return;
      block.request({
        number: entry.fromNumber,
        label: entry.fromName ?? formatPhoneNumber(entry.fromNumber),
        isFavorite: entry.isFavorite,
      });
    },
    [block],
  );

  const nameFromCall = useCallback(
    (call: Call) => {
      if (!call.caller.number) return;
      contacts.requestName({
        number: call.caller.number,
        label: formatPhoneNumber(call.caller.number),
        currentName: call.caller.name ?? '',
      });
    },
    [contacts],
  );

  const nameFromHistory = useCallback(
    (entry: CallHistoryEntry) => {
      if (!entry.fromNumber) return;
      contacts.requestName({
        number: entry.fromNumber,
        label: formatPhoneNumber(entry.fromNumber),
        currentName: entry.fromName ?? '',
      });
    },
    [contacts],
  );

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <h1>Call Screener</h1>
          <p>Call screening and moderation</p>
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

        <ConnectionBadge status={connection} />
      </header>

      {lastError && (
        <div className="alert" role="alert">
          <span>{lastError}</span>
          <button type="button" className="alert__dismiss" onClick={dismissError}>
            Dismiss
          </button>
        </div>
      )}

      {contacts.error && (
        <div className="alert" role="alert">
          <span>{contacts.error}</span>
          <button type="button" className="alert__dismiss" onClick={contacts.dismissError}>
            Dismiss
          </button>
        </div>
      )}

      {block.lastResult && (
        <div className="alert alert--success" role="status">
          <span>{describeBlockResult(block.lastResult)}</span>
          <button type="button" className="alert__dismiss" onClick={block.clearResult}>
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
                  Incoming calls appear here once the caller has said why they’re calling.
                  {connection !== 'open' && ' Reconnecting to the call stream…'}
                </p>
              </div>
            ) : (
              <>
                <header className="detail__header">
                  <div>
                    <h2 className="detail__caller">
                      <FavoriteStar
                        isFavorite={selectedCall.caller.isFavorite}
                        disabled={!selectedCall.caller.number}
                        pending={contacts.pendingNumber === selectedCall.caller.number}
                        onToggle={() =>
                          selectedCall.caller.number &&
                          contacts.toggleFavorite(
                            selectedCall.caller.number,
                            !selectedCall.caller.isFavorite,
                          )
                        }
                        label={
                          selectedCall.caller.name ??
                          formatPhoneNumber(selectedCall.caller.number)
                        }
                      />
                      <button
                        type="button"
                        className="detail__name-button"
                        onClick={() => nameFromCall(selectedCall)}
                        disabled={!selectedCall.caller.number}
                        title={
                          selectedCall.caller.number
                            ? 'Name this caller'
                            : 'Caller ID was withheld'
                        }
                      >
                        {selectedCall.caller.name ??
                          formatPhoneNumber(selectedCall.caller.number)}
                      </button>
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
                  onBlock={requestBlockFromCall}
                />
              </>
            )}
          </section>
        </main>
      ) : (
        <main className="app__body app__body--single">
          <CallHistoryView
            entries={history.entries}
            loading={history.loading}
            error={history.error}
            onRefresh={history.refresh}
            onBlock={requestBlockFromHistory}
            onToggleFavorite={(entry) =>
              entry.fromNumber && contacts.toggleFavorite(entry.fromNumber, !entry.isFavorite)
            }
            onName={nameFromHistory}
            pendingNumber={contacts.pendingNumber}
          />
        </main>
      )}

      <ConfirmDialog
        open={block.target !== null}
        title="Block this caller?"
        message={
          <>
            {/* Someone deliberately starred this caller. Blocking them is
                probably a misclick, and this is the last chance to catch it. */}
            {block.target?.isFavorite && (
              <p className="dialog__alarm">
                ★ This caller is a favourite.
              </p>
            )}
            <p>
              Are you sure you want to block <strong>{block.target?.label}</strong>?
            </p>
            <p className="dialog__detail">
              {block.target?.callId
                ? 'Their call will be hung up immediately and future calls will be refused before they ring.'
                : 'Future calls from this number will be refused before they ring.'}{' '}
              This cannot be undone from the dashboard.
            </p>
          </>
        }
        confirmLabel="Block caller"
        destructive
        pending={block.pending}
        error={block.error}
        onConfirm={block.confirm}
        onCancel={block.cancel}
      />

      <NameContactDialog
        target={contacts.nameTarget}
        pending={contacts.namePending}
        error={contacts.nameError}
        onSave={contacts.saveName}
        onClear={contacts.clearName}
        onCancel={contacts.cancelName}
      />
    </div>
  );
}

function describeBlockResult(result: {
  blocked: { number: string };
  newlyBlocked: boolean;
  terminatedCallIds: string[];
  failedCallIds: string[];
}): string {
  const number = formatPhoneNumber(result.blocked.number);
  // Report the hang-up separately from the block: they fail independently, and
  // "blocked" without "dropped" would let a moderator assume the caller is
  // gone when they are still connected.
  if (result.failedCallIds.length > 0) {
    return `${number} was blocked, but their live call could not be hung up. Check the call manually.`;
  }
  if (result.terminatedCallIds.length > 0) {
    return `${number} was blocked and their live call was dropped.`;
  }
  return result.newlyBlocked
    ? `${number} was blocked. Future calls will be refused.`
    : `${number} was already blocked.`;
}
