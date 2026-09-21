import { statusLabel } from '../lib/format';
import type { Call } from '../types/events';
import { isTerminal } from '../types/events';

interface Props {
  call: Call;
  pending: boolean;
  onAccept: (callId: string) => void;
  onReject: (callId: string) => void;
}

/**
 * The decisions an operator can make about the active call.
 *
 * `POST /api/calls/{id}/accept|reject`. Both reach Twilio before the status
 * changes, so a failure leaves the call on the dashboard — see
 * `backend/app/services/decisions.py`.
 */
export default function CallActions({ call, pending, onAccept, onReject }: Props) {
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
    </div>
  );
}

const RESOLUTION_HINTS: Partial<Record<Call['status'], string>> = {
  accepted: 'Call was connected.',
  rejected: 'Call was declined.',
  ended: 'Caller hung up.',
};
