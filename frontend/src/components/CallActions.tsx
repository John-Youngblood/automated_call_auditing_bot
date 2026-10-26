import { statusLabel } from '../lib/format';
import type { Call } from '../types/events';
import { isTerminal } from '../types/events';

interface Props {
  call: Call;
  pending: boolean;
  onAccept: (callId: string) => void;
  onReject: (callId: string) => void;
  onBlock: (number: string) => void;
}

/**
 * The decisions an operator can make about the active call.
 *
 * `POST /api/calls/{id}/accept|reject`. Both reach Twilio before the status
 * changes, so a failure leaves the call on the dashboard — see
 * `backend/app/services/decisions.py`.
 *
 * Blocking is quieter and last: it outlives this call, so it asks first.
 */
export default function CallActions({ call, pending, onAccept, onReject, onBlock }: Props) {
  const { number } = call.caller;

  if (isTerminal(call.status)) {
    return (
      <div className="actions actions--resolved">
        <span className={`status status--${call.status} status--large`}>
          {statusLabel(call.status)}
        </span>
        <span className="actions__hint">{RESOLUTION_HINTS[call.status] ?? 'Call ended.'}</span>
      </div>
    );
  }

  return (
    <div className="actions">
      <button
        type="button"
        className="button button--accept"
        onClick={() => onAccept(call.callId)}
        disabled={pending}
      >
        {pending ? 'Working…' : 'Accept Call'}
      </button>
      <button
        type="button"
        className="button button--reject"
        onClick={() => onReject(call.callId)}
        disabled={pending}
      >
        {pending ? 'Working…' : 'Reject Call'}
      </button>
      {number && (
        <button
          type="button"
          className="button button--ghost button--compact"
          onClick={() => onBlock(number)}
          disabled={pending}
        >
          Block number…
        </button>
      )}
    </div>
  );
}

const RESOLUTION_HINTS: Partial<Record<Call['status'], string>> = {
  accepted: 'Call was connected.',
  rejected: 'Call was declined.',
  ended: 'Caller hung up.',
};
