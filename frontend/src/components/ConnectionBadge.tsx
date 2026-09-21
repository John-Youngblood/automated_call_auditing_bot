import type { ConnectionStatus } from '../types/events';

/**
 * "Connected", not "Live". This badge is about the socket to the backend, and
 * sitting next to the line-state pill "Live" read as a second opinion on
 * whether the show was taking calls.
 */
const LABELS: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  open: 'Connected',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
};

/**
 * Connection state is load-bearing on a screening dashboard: an empty queue
 * and a broken socket look identical otherwise, and an operator would sit
 * watching a dead page believing nobody is calling.
 *
 * Deliberately quiet while healthy and loud when not. It has to stay visible
 * -- absence would be as ambiguous as the problem it solves -- but it is
 * plumbing, and the pill beside it carries the state an operator is actually
 * working from.
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
