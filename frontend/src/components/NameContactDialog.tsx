import { useEffect, useRef, useState } from 'react';

import type { NameTarget } from '../hooks/useContacts';

interface Props {
  target: NameTarget | null;
  pending: boolean;
  error: string | null;
  onSave: (name: string) => void;
  onClear: () => void;
  onCancel: () => void;
}

/**
 * Small form for putting a name on a number.
 *
 * Native `<dialog>` again, for the same reasons as the block confirmation —
 * focus trapping, Escape and focus restoration come from the platform. Unlike
 * that one, focus starts in the text field: this action is reversible and
 * typing is the whole point.
 */
export default function NameContactDialog({
  target,
  pending,
  error,
  onSave,
  onClear,
  onCancel,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState('');

  const open = target !== null;

  useEffect(() => {
    if (target) setName(target.currentName);
  }, [target]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open && !dialog.open) {
      dialog.showModal();
      inputRef.current?.focus();
      inputRef.current?.select();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const handleCancel = (event: Event) => {
      event.preventDefault(); // React state stays the source of truth
      if (!pending) onCancel();
    };
    dialog.addEventListener('cancel', handleCancel);
    return () => dialog.removeEventListener('cancel', handleCancel);
  }, [onCancel, pending]);

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="name-dialog-title"
      onClick={(event) => {
        if (event.target === dialogRef.current && !pending) onCancel();
      }}
    >
      <form
        className="dialog__body"
        onSubmit={(event) => {
          event.preventDefault();
          onSave(name);
        }}
      >
        <h2 id="name-dialog-title" className="dialog__title">
          Name this caller
        </h2>
        <p className="dialog__message dialog__detail">
          Calls from <strong>{target?.label}</strong> will show this name instead of the number.
        </p>

        <label className="field">
          <span className="field__label">Name</span>
          <input
            ref={inputRef}
            className="field__input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Dana Rivera"
            maxLength={128}
            disabled={pending}
            autoComplete="off"
          />
        </label>

        {error && (
          <p className="dialog__error" role="alert">
            {error}
          </p>
        )}

        <div className="dialog__actions">
          {target?.currentName && (
            <button
              type="button"
              className="button button--ghost button--quiet"
              onClick={onClear}
              disabled={pending}
            >
              Remove name
            </button>
          )}
          <button
            type="button"
            className="button button--ghost"
            onClick={onCancel}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="button button--accept"
            disabled={pending || name.trim() === target?.currentName.trim()}
          >
            {pending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
