import type { ConnectionStatus } from '../types/events';

const LABELS: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
};

/**
 * Connection state is load-bearing on a screening dashboard: an empty queue
 * and a broken socket look identical otherwise, and an operator would sit
 * watching a dead page believing nobody is calling.
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
