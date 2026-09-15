import { formatElapsed, formatPhoneNumber } from '../lib/format';
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
  // The latest line doubles as a preview, so an agent can triage from the list
  // without opening every call.
  const latest = call.transcript.at(-1);

  return (
    <li>
      <button
        type="button"
        className={`call-row${selected ? ' call-row--selected' : ''}`}
        onClick={() => onSelect(call.callId)}
        aria-current={selected}
      >
        <span className="call-row__top">
          <span className="call-row__number">{formatPhoneNumber(call.caller.number)}</span>
          {/* Counts up live while the call is open, then freezes at its
              total duration once it ends. */}
          <span className="call-row__timer">
            {formatElapsed(
              call.startedAt,
              call.endedAt ? new Date(call.endedAt).getTime() : now,
            )}
          </span>
        </span>
        <span className="call-row__meta">
          <span className={`status status--${call.status}`}>{call.status}</span>
          {call.caller.name && <span className="call-row__name">{call.caller.name}</span>}
        </span>
        {latest && <span className="call-row__preview">{latest.text}</span>}
      </button>
    </li>
  );
}
