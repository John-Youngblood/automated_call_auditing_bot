import { formatClock, formatDuration, formatPhoneNumber, statusLabel } from '../lib/format';
import type { Call } from '../types/events';

interface Props {
  calls: Call[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

/**
 * Past calls: accepted, rejected and dropped.
 *
 * Separate from the live queue on purpose. The queue is a work surface -- a
 * few rows demanding a decision in the next few seconds. History is a record,
 * read at leisure, and mixing the two makes the urgent rows harder to find.
 *
 * The server keeps these in memory only, so the list starts empty after a
 * backend restart. The subtitle says so rather than leaving an operator to
 * wonder where the morning's calls went.
 */
export default function CallHistoryView({ calls, loading, error, onRefresh }: Props) {
  return (
    <section className="history" aria-label="Call history">
      <header className="history__header">
        <div>
          <h2>Call History</h2>
          <p className="history__subtitle">
            {calls.length > 0
              ? `${calls.length} recent call${calls.length === 1 ? '' : 's'} · cleared when the server restarts`
              : 'Accepted, rejected and dropped calls'}
          </p>
        </div>
        <button type="button" className="button button--ghost button--compact" onClick={onRefresh}>
          Refresh
        </button>
      </header>

      {error && (
        <p className="history__error" role="alert">
          {error}
        </p>
      )}

      {loading && calls.length === 0 ? (
        <p className="history__empty">Loading…</p>
      ) : calls.length === 0 ? (
        <p className="history__empty">No calls yet. Finished calls appear here.</p>
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
                <HistoryRow key={call.callId} call={call} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function HistoryRow({ call }: { call: Call }) {
  const duration = durationSeconds(call);

  return (
    <tr>
      <td className="history__time">
        <span>{formatClock(call.startedAt)}</span>
        <span className="history__date">{formatHistoryDate(call.startedAt)}</span>
      </td>

      <td>
        {/* Carrier caller-ID name when there is one, with the number below it;
            otherwise the number is the heading. */}
        <span className="history__number">
          {call.caller.name ?? formatPhoneNumber(call.caller.number)}
        </span>
        <span className="history__caller-meta">
          {[call.caller.name ? formatPhoneNumber(call.caller.number) : null, call.caller.location]
            .filter(Boolean)
            .join(' · ')}
        </span>
      </td>

      <td>
        <span className={`status status--${call.status}`}>{statusLabel(call.status)}</span>
        {duration !== null && <span className="history__duration">{formatDuration(duration)}</span>}
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

/** How long the call was being screened, or null while it is still open. */
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
  return sameDay ? 'Today' : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
