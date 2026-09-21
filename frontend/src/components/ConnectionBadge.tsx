import type { ConnectionStatus } from '../types/events';

/** "Connected", not "Live" — that read as a second opinion on the line state. */
const LABELS: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  open: 'Connected',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
};

/**
 * Load-bearing: an empty queue and a broken socket look identical otherwise,
 * so an operator could sit watching a dead page believing nobody is calling.
 * Quiet while healthy, loud when not.
 */
export default function ConnectionBadge({ status }: { status: ConnectionStatus }) {
  return (
    <div className="badges">
      <span className={`badge badge--${status}`}>
        <span className="badge__dot" aria-hidden="true" />
        {LABELS[status]}
      </span>
    </div>
  );
}
