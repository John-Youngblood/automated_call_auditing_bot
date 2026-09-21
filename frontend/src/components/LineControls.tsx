import { useState } from 'react';

import type { ToastTone } from '../hooks/useToasts';
import { ApiError, callsApi } from '../lib/api';
import type { LineStateResult } from '../types/events';
import ConfirmDialog from './ConfirmDialog';

interface Props {
  open: boolean;
  onNotice: (message: string, tone: ToastTone) => void;
}

/**
 * Going on and off air.
 *
 * One action in each direction. Closing the line turns new callers away *and*
 * hangs up on anyone still holding, always — there is no version of ending a
 * show that leaves people waiting on a line nobody is watching.
 *
 * Closing confirms, opening does not. The confirmation is a modal rather than
 * a second click on the button, because what closing does is invisible: the
 * callers it turns away are ones the operator will never see, and a button
 * label cannot explain that the people on hold are about to be disconnected.
 */
export default function LineControls({ open, onNotice }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);

  async function setLine(next: boolean) {
    setPending(true);
    try {
      const { message, tone } = describe(await callsApi.setLineOpen(next));
      onNotice(message, tone);
      setConfirming(false);
    } catch (err: unknown) {
      const what = next ? 'open' : 'close';
      onNotice(
        err instanceof ApiError
          ? `Could not ${what} the line: ${err.message}`
          : `Could not ${what} the line.`,
        'warn',
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <span className="line-controls">
      <button
        type="button"
        className="button button--ghost button--compact"
        onClick={() => (open ? setConfirming(true) : setLine(true))}
        disabled={pending}
      >
        {pending && !confirming ? 'Working…' : open ? 'Close the line' : 'Open the line'}
      </button>

      <span className={`line-state${open ? ' line-state--open' : ''}`}>
        <span className="line-state__dot" aria-hidden="true" />
        {open ? 'Line open' : 'Line closed'}
      </span>

      <ConfirmDialog
        open={confirming}
        title="Close the line?"
        message={
          <>
            <p>New callers will be told the show is not taking calls, and hung up.</p>
            <p>
              Anyone still on hold will be played a goodbye message and disconnected. This
              cannot be undone — reopening the line will not bring them back.
            </p>
          </>
        }
        confirmLabel="Close the line"
        pending={pending}
        onConfirm={() => setLine(false)}
        onCancel={() => setConfirming(false)}
      />
    </span>
  );
}

function describe(result: LineStateResult): { message: string; tone: ToastTone } {
  if (result.open) {
    return { message: 'Line open. New callers will be screened.', tone: 'ok' };
  }

  // A warning, not a confirmation: those callers are still connected, so this
  // one must not expire on its own while the operator is looking elsewhere.
  if (result.failedCallIds.length > 0) {
    return {
      message:
        `Line closed. Hung up ${result.endedCallIds.length}, but ` +
        `${result.failedCallIds.length} could not be reached and may still be holding. ` +
        `Check them manually.`,
      tone: 'warn',
    };
  }
  const n = result.endedCallIds.length;
  return {
    message:
      n === 0
        ? 'Line closed. Nobody was holding.'
        : `Line closed. ${n} caller${n === 1 ? '' : 's'} were told the show has ended.`,
    tone: 'ok',
  };
}
