import type { ConnectionStatus } from '../types/events';

const LABELS: Record<ConnectionStatus, string> = {
  connecting: 'Connecting',
  open: 'Live',
  reconnecting: 'Reconnecting',
  closed: 'Disconnected',
};

interface Props {
  status: ConnectionStatus;
  /** Transcription health for the selected call. */
  sttState: string | null;
}

/**
 * Connection state is load-bearing on a screening dashboard: an empty queue
 * and a broken socket look identical otherwise, and an agent would sit
 * watching a dead page believing nobody is calling.
 *
 * Transcription health is shown only when it is `degraded`. `connected` and
 * `closed` are ordinary lifecycle events -- surfacing them trains operators to
 * ignore the badge, which is exactly when they stop noticing a real outage.
 */
export default function ConnectionBadge({ status, sttState }: Props) {
  const transcriptionDegraded = sttState === 'degraded';

  return (
    <div className="badges">
      {transcriptionDegraded && (
        <span
          className="badge badge--closed"
          role="status"
          title="Audio is still flowing, but speech-to-text is not returning results"
        >
          <span className="badge__dot" aria-hidden="true" />
          Transcription degraded
        </span>
      )}
      <span className={`badge badge--${status}`}>
        <span className="badge__dot" aria-hidden="true" />
        {LABELS[status]}
      </span>
    </div>
  );
}
