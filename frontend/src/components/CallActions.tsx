import { statusLabel } from '../lib/format';
import type { Call } from '../types/events';
import { isTerminal } from '../types/events';

interface Props {
  call: Call;
  pending: boolean;
  onAccept: (callId: string) => void;
  onReject: (callId: string) => void;
  onBlock: (call: Call) => void;
}

/**
 * The decisions an operator can make about the active call.
 *
 * Accept and Reject act on *this call*. Block acts on the *caller* -- it drops
 * the live call and bars every future one. That difference is why Block is
 * visually separated and is the only one that asks for confirmation.
 *
 * Accept and Reject hit placeholder backend endpoints
 * (`POST /api/calls/{id}/accept|reject`): the status change and its broadcast
 * to every dashboard are real, while bridging or declining at the carrier is
 * still a stub. See `backend/app/telephony/provider_client.py`.
 */
export default function CallActions({ call, pending, onAccept, onReject, onBlock }: Props) {
  const decided = isTerminal(call.status);
  // Nothing to block when caller ID was withheld -- there is no number to add.
  const canBlock = Boolean(call.caller.number);

  if (decided) {
    return (
      <div className="actions actions--resolved">
        <span className={`status status--${call.status} status--large`}>
          {statusLabel(call.status)}
        </span>
        <span className="actions__hint">{RESOLUTION_HINTS[call.status] ?? 'Call ended.'}</span>
        {canBlock && call.status !== 'blocked' && (
          <button
            type="button"
            className="button button--danger button--compact actions__late-block"
            onClick={() => onBlock(call)}
          >
            Block caller
          </button>
        )}
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
      {/* A thematic break, not decoration: everything above decides this call,
          everything below bars the caller from every future one. */}
      <hr className="actions__rule" />

      <button
        type="button"
        className="button button--danger button--block"
        onClick={() => onBlock(call)}
        disabled={pending || !canBlock}
        title={
          canBlock
            ? 'Hang up and bar this caller from calling again'
            : 'Caller ID was withheld, so there is no number to block'
        }
      >
        Block
      </button>
    </div>
  );
}

const RESOLUTION_HINTS: Partial<Record<Call['status'], string>> = {
  accepted: 'Call was connected.',
  rejected: 'Call was declined.',
  blocked: 'Caller was blocked.',
  ended: 'Caller hung up.',
};
