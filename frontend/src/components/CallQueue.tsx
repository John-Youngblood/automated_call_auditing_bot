import { formatElapsed, formatPhoneNumber, statusLabel } from '../lib/format';
import type { Call } from '../types/events';

interface Props {
  activeCalls: Call[];
  recentCalls: Call[];
  selectedCallId: string | null;
  onSelect: (callId: string) => void;
  now: number;
}

export default function CallQueue({
  activeCalls,
  recentCalls,
  selectedCallId,
  onSelect,
  now,
}: Props) {
  return (
    <aside className="queue" aria-label="Incoming call queue">
      <header className="queue__header">
        <h2>Incoming</h2>
        <span className="queue__count" aria-label={`${activeCalls.length} calls waiting`}>
          {activeCalls.length}
        </span>
      </header>

      {activeCalls.length === 0 ? (
        <p className="queue__empty">No calls in progress.</p>
      ) : (
        <ul className="queue__list">
          {activeCalls.map((call) => (
            <QueueRow
              key={call.callId}
              call={call}
              selected={call.callId === selectedCallId}
              onSelect={onSelect}
              now={now}
            />
          ))}
        </ul>
      )}

      {recentCalls.length > 0 && (
        <>
          <header className="queue__header queue__header--sub">
            <h2>Recent</h2>
          </header>
          <ul className="queue__list queue__list--recent">
            {recentCalls.slice(0, 8).map((call) => (
              <QueueRow
                key={call.callId}
                call={call}
                selected={call.callId === selectedCallId}
                onSelect={onSelect}
                now={now}
              />
            ))}
          </ul>
        </>
      )}
    </aside>
  );
}

interface RowProps {
  call: Call;
  selected: boolean;
  onSelect: (callId: string) => void;
  now: number;
}

function QueueRow({ call, selected, onSelect, now }: RowProps) {
  // The transcript doubles as a preview, so an operator can triage from the
  // list without opening every call.
  const preview = call.transcript ?? (call.status === 'screening' ? 'Still speaking…' : null);

  return (
    <li>
      <button
        type="button"
        className={`call-row${selected ? ' call-row--selected' : ''}`}
        onClick={() => onSelect(call.callId)}
        aria-current={selected}
      >
        {/* Head and preview sit side by side on a wide queue and stack when it
            narrows -- flex-wrap handles both without a media query. */}
        <span className="call-row__head">
          <span className="call-row__identity">
            <span className="call-row__number">
              {call.caller.name ?? formatPhoneNumber(call.caller.number)}
            </span>
            <span className="call-row__meta">
              <span className={`status status--${call.status}`}>{statusLabel(call.status)}</span>
              {/* Name replaces the number above, so show the number here --
                  an operator often needs to read it out or cross-reference it. */}
              {call.caller.name && call.caller.number && (
                <span className="call-row__alt">{formatPhoneNumber(call.caller.number)}</span>
              )}
            </span>
          </span>

          {/* Counts up live while the call is open, then freezes at its
              total duration once it ends. */}
          <span className="call-row__timer">
            {formatElapsed(
              call.startedAt,
              call.endedAt ? new Date(call.endedAt).getTime() : now,
            )}
          </span>
        </span>

        {preview && <span className="call-row__preview">{preview}</span>}
      </button>
    </li>
  );
}
