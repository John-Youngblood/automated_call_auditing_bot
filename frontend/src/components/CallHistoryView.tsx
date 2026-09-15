import { formatClock, formatDuration, formatPhoneNumber } from '../lib/format';
import type { CallHistoryEntry } from '../types/events';

interface Props {
  entries: CallHistoryEntry[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onBlock: (entry: CallHistoryEntry) => void;
}

/**
 * Past calls: completed, rejected, dropped and blocked.
 *
 * Separate from the live queue on purpose. The queue is a work surface -- a
 * few rows demanding a decision in the next few seconds. History is a record,
 * read at leisure, and mixing the two makes the urgent rows harder to find.
 *
 * Every row carries a Block action so a moderator can bar a caller who has
 * already hung up, which is the common case: by the time you decide someone
 * needs blocking, the call is usually over.
 */
export default function CallHistoryView({ entries, loading, error, onRefresh, onBlock }: Props) {
  return (
    <section className="history" aria-label="Call history">
      <header className="history__header">
        <div>
          <h2>Call History</h2>
          <p className="history__subtitle">
            {entries.length > 0
              ? `${entries.length} recent call${entries.length === 1 ? '' : 's'}`
              : 'Completed, rejected, dropped and blocked calls'}
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

      {loading && entries.length === 0 ? (
        <p className="history__empty">Loading…</p>
      ) : entries.length === 0 ? (
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
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <HistoryRow key={entry.callId} entry={entry} onBlock={onBlock} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function HistoryRow({
  entry,
  onBlock,
}: {
  entry: CallHistoryEntry;
  onBlock: (entry: CallHistoryEntry) => void;
}) {
  const canBlock = Boolean(entry.fromNumber) && !entry.isBlocked;

  return (
    <tr>
      <td className="history__time">
        <span>{formatClock(entry.startedAt)}</span>
        <span className="history__date">{formatHistoryDate(entry.startedAt)}</span>
      </td>

      <td>
        <span className="history__number">{formatPhoneNumber(entry.fromNumber)}</span>
        {(entry.fromName || entry.fromLocation) && (
          <span className="history__caller-meta">
            {[entry.fromName, entry.fromLocation].filter(Boolean).join(' · ')}
          </span>
        )}
      </td>

      <td>
        <span className={`status status--${entry.status}`}>{entry.status}</span>
        {entry.durationSeconds !== null && (
          <span className="history__duration">{formatDuration(entry.durationSeconds)}</span>
        )}
      </td>

      <td className="history__transcript">
        {entry.transcriptSummary ? (
          <>
            <span>{entry.transcriptSummary}</span>
            {entry.transcriptLineCount > 0 && (
              <span className="history__lines">{entry.transcriptLineCount} lines</span>
            )}
          </>
        ) : (
          <span className="history__no-transcript">No transcript</span>
        )}
      </td>

      <td className="history__actions">
        {entry.isBlocked ? (
          <span className="badge badge--closed" title="This caller is on the blocklist">
            Blocked
          </span>
        ) : (
          <button
            type="button"
            className="button button--danger button--compact"
            onClick={() => onBlock(entry)}
            disabled={!canBlock}
            title={
              canBlock
                ? 'Bar this caller from calling again'
                : 'Caller ID was withheld, so there is no number to block'
            }
          >
            Block
          </button>
        )}
      </td>
    </tr>
  );
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
