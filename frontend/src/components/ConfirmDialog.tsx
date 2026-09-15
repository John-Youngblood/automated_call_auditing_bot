import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

interface Props {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Styles the confirm button as dangerous and keeps focus off it. */
  destructive?: boolean;
  pending?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirmation dialog built on the native `<dialog>` element.
 *
 * Using the platform element rather than a div-with-a-backdrop is what gets
 * focus trapping, Escape-to-dismiss, focus restoration to the trigger, and
 * inert-ing of the page behind it -- all correct, all for free, none of it
 * hand-rolled.
 *
 * One deliberate choice for a destructive action: focus starts on **Cancel**,
 * not Confirm. A moderator hammering Enter during a live show should not be
 * able to block a caller they never meant to touch.
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  pending = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    // showModal() throws if the dialog is already open, and close() on a
    // closed dialog is a no-op, so both need the guard.
    if (open && !dialog.open) {
      dialog.showModal();
      cancelRef.current?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    // Fires on Escape. Prevent the default close so React state stays the
    // source of truth for whether the dialog is open.
    const handleCancel = (event: Event) => {
      event.preventDefault();
      if (!pending) onCancel();
    };
    dialog.addEventListener('cancel', handleCancel);
    return () => dialog.removeEventListener('cancel', handleCancel);
  }, [onCancel, pending]);

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="confirm-dialog-title"
      // Clicking the backdrop cancels. Safe by definition: the backdrop can
      // only ever dismiss, never confirm.
      onClick={(event) => {
        if (event.target === dialogRef.current && !pending) onCancel();
      }}
    >
      <div className="dialog__body">
        <h2 id="confirm-dialog-title" className="dialog__title">
          {title}
        </h2>
        <div className="dialog__message">{message}</div>

        {error && (
          <p className="dialog__error" role="alert">
            {error}
          </p>
        )}

        <div className="dialog__actions">
          <button
            type="button"
            ref={cancelRef}
            className="button button--ghost"
            onClick={onCancel}
            disabled={pending}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`button ${destructive ? 'button--danger' : 'button--accept'}`}
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
