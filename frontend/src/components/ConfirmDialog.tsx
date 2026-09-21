import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

interface Props {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel: string;
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A modal confirmation, on the native `<dialog>` element.
 *
 * Native rather than hand-rolled: the browser gives focus trapping, Escape to
 * dismiss, inertness of the page behind and the top layer for free, and every
 * one of those is easy to get subtly wrong by hand.
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel,
  pending = false,
  onConfirm,
  onCancel,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // showModal() throws if already open, and close() if already closed.
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="dialog"
      // Escape and backdrop dismissal both surface as `cancel`. Suppressed
      // while the request is in flight: closing then would leave the operator
      // with no idea whether the callers were hung up.
      onCancel={(event) => {
        if (pending) event.preventDefault();
        else onCancel();
      }}
    >
      <div className="dialog__body">
        <h2 className="dialog__title">{title}</h2>
        <div className="dialog__message">{message}</div>
        <div className="dialog__actions">
          <button
            type="button"
            className="button button--ghost button--compact"
            onClick={onCancel}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="button button--danger button--compact"
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
