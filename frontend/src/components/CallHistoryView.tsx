import type { CallHistory } from '../hooks/useCallHistory';
import {
  callerLabel,
  formatClock,
  formatDuration,
  formatPhoneNumber,
  formatShortDate,
  numberUnderName,
  statusLabel,
} from '../lib/format';
import type { Call, Contact } from '../types/events';

interface Props {
  history: CallHistory;
  /** Shown in place of "Saved on this machine" when that stops being true. */
  storageWarning: string | null;
  onEditContact: (number: string, contact: Contact | null) => void;
  onBlock: (number: string) => void;
  onUnblock: (number: string) => void;
}

/**
 * Past calls: accepted, rejected and dropped.
 *
 * Separate from the live queue on purpose. The queue is a work surface -- a
 * few rows demanding a decision in the next few seconds. History is a record,
 * read at leisure, and mixing the two makes the urgent rows harder to find.
 *
 * Kept on disk by the backend, so it outlives a restart. The subtitle says so,
 * or says why not when storage has failed, rather than leaving an operator to
 * wonder where the morning's calls went.
 */
export default function CallHistoryView({
  history,
  storageWarning,
  onEditContact,
  onBlock,
  onUnblock,
}: Props) {
  const { calls, loading, error } = history;
  const filtered = history.number !== null || history.query.trim() !== '';

  return (
    <section className="history" aria-label="Call history">
      <header className="history__header">
        <div>
          <h2>Call History</h2>
          <p className={`history__subtitle${storageWarning ? ' history__subtitle--warning' : ''}`}>
            {storageWarning ?? 'Saved on this machine'}
          </p>
        </div>
        <button
          type="button"
          className="button button--ghost button--compact"
          onClick={history.refresh}
        >
          Refresh
        </button>
      </header>

      <div className="toolbar">
        <input
          className="input toolbar__search"
          type="search"
          value={history.query}
          onChange={(event) => history.setQuery(event.target.value)}
          placeholder="Search what callers said, names, notes, numbers"
          aria-label="Search call history"
        />
        {history.number && (
          <span className="chip chip--active">
            {formatPhoneNumber(history.number)}
            <button
              type="button"
              className="chip__remove"
              onClick={() => history.showNumber(null)}
              aria-label="Show calls from every number"
            >
              ×
            </button>
          </span>
        )}
      </div>

      {error && (
        <p className="history__error" role="alert">
          {error}
        </p>
      )}

      {loading && calls.length === 0 ? (
        <p className="history__empty">Loading…</p>
      ) : calls.length === 0 ? (
        <p className="history__empty">
          {filtered ? 'No calls match.' : 'No calls yet. Finished calls appear here.'}
        </p>
      ) : (
        <div className="history__scroll">
          <table className="history__table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Caller</th>
                <th scope="col">Status</th>
                <th scope="col">Transcript</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((call) => (
                <HistoryRow
                  key={call.callId}
                  call={call}
                  onEditContact={onEditContact}
                  onBlock={onBlock}
                  onUnblock={onUnblock}
                />
              ))}
            </tbody>
          </table>

          {history.hasMore && (
            <div className="history__more">
              <button
                type="button"
                className="button button--ghost button--compact"
                onClick={history.loadOlder}
                disabled={history.loadingOlder}
              >
                {history.loadingOlder ? 'Loading…' : 'Load older calls'}
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

interface RowProps {
  call: Call;
  onEditContact: (number: string, contact: Contact | null) => void;
  onBlock: (number: string) => void;
  onUnblock: (number: string) => void;
}

function HistoryRow({ call, onEditContact, onBlock, onUnblock }: RowProps) {
  const duration = durationSeconds(call);
  const { number, contact } = call.caller;
  const blocked = contact?.standing === 'blocked';

  return (
    <tr>
      <td className="history__time">
        <span>{formatClock(call.startedAt)}</span>
        <span className="history__date">{formatHistoryDate(call.startedAt)}</span>
      </td>

      <td>
        {/* A name when there is one, with the number below it; otherwise the
            number is the heading. */}
        <span className="history__number">
          {contact?.standing === 'favorite' && (
            <span className="star" role="img" aria-label="Favorite">
              ★
            </span>
          )}
          {callerLabel(call.caller)}
          {blocked && <span className="status status--blocked">blocked</span>}
        </span>
        <span className="history__caller-meta">
          {[numberUnderName(call.caller), call.caller.location].filter(Boolean).join(' · ')}
        </span>
        {/* A withheld number has nothing to save a contact against. */}
        {number && (
          <span className="history__actions">
            <button type="button" className="link" onClick={() => onEditContact(number, contact)}>
              {contact ? 'Edit contact' : 'Add contact'}
            </button>
            {blocked ? (
              <button type="button" className="link" onClick={() => onUnblock(number)}>
                Unblock
              </button>
            ) : (
              <button type="button" className="link" onClick={() => onBlock(number)}>
                Block
              </button>
            )}
          </span>
        )}
      </td>

      <td>
        <span className={`status status--${call.status}`}>{statusLabel(call.status)}</span>
        {duration !== null && <span className="history__duration">{formatDuration(duration)}</span>}
        {/* Closed by the startup sweep: the backend was down when it ended, so
            nobody knows when, and a duration would be a guess. */}
        {call.status === 'ended' && call.endedAt === null && (
          <span className="history__restart">ended during a restart</span>
        )}
        {/* `ended` is where every finished call lands, so on its own it cannot
            say whether this one made it on air. */}
        {call.wasAccepted && (
          <span
            className={`history__on-air${call.onAirSeconds === null ? ' history__on-air--missed' : ''}`}
          >
            {call.onAirSeconds === null
              ? 'never connected'
              : `on air ${formatDuration(call.onAirSeconds)}`}
          </span>
        )}
      </td>

      <td className="history__transcript">
        {call.transcript ? (
          <span>{call.transcript}</span>
        ) : (
          <span className="history__no-transcript">No transcript</span>
        )}
      </td>
    </tr>
  );
}

/** How long the call was being screened, or null when that is unknown. */
function durationSeconds(call: Call): number | null {
  if (!call.endedAt) return null;
  const seconds = (new Date(call.endedAt).getTime() - new Date(call.startedAt).getTime()) / 1000;
  return Math.max(0, Math.round(seconds));
}

function formatHistoryDate(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  return sameDay ? 'Today' : formatShortDate(iso);
}
