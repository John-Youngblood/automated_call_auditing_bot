import { useEffect, useId, useRef, useState } from 'react';

import type { ContactDialogState } from '../hooks/useContactActions';
import { formatPhoneNumber } from '../lib/format';
import type { Contact, ContactPatch, ContactStanding } from '../types/events';

/** The server's limits, so the browser stops the typing instead of a 422. */
const NAME_MAX = 80;
const NOTE_MAX = 500;

const STANDINGS: { value: ContactStanding | null; label: string }[] = [
  { value: null, label: 'None' },
  { value: 'favorite', label: 'Favorite' },
  { value: 'blocked', label: 'Blocked' },
];

/**
 * Name, note and standing for one number, on the native `<dialog>` element
 * like ConfirmDialog and for the same reasons.
 *
 * Sends only the fields that changed, so two operators editing the same
 * contact -- one the name, one the note -- do not undo each other.
 */
export default function ContactDialog(props: ContactDialogState) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const { open, pending, onCancel } = props;

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
      className="dialog dialog--form"
      aria-labelledby={titleId}
      onCancel={(event) => {
        if (pending) event.preventDefault();
        else onCancel();
      }}
    >
      {/* Mounted per opening, so every edit starts from the contact as it is
          now rather than from whatever was typed last time. */}
      {open && <ContactForm {...props} titleId={titleId} />}
    </dialog>
  );
}

function ContactForm({
  number,
  contact,
  pending,
  error,
  onSave,
  onRemove,
  onCancel,
  titleId,
}: ContactDialogState & { titleId: string }) {
  const [typedNumber, setTypedNumber] = useState('');
  const [name, setName] = useState(contact?.name ?? '');
  const [note, setNote] = useState(contact?.note ?? '');
  const [standing, setStanding] = useState<ContactStanding | null>(contact?.standing ?? null);

  const target = number ?? typedNumber.trim();
  const patch = changedFields(contact, name, note, standing);
  const canSave = target !== '' && Object.keys(patch).length > 0 && !pending;

  return (
    <form
      className="dialog__body"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSave) onSave(target, patch);
      }}
    >
      <h2 className="dialog__title" id={titleId}>
        {contact ? 'Edit contact' : 'Add contact'}
      </h2>

      {number ? (
        <div className="field">
          <span className="field__label">Number</span>
          <span className="field__value">{formatPhoneNumber(number)}</span>
        </div>
      ) : (
        <label className="field">
          <span className="field__label">Number</span>
          <input
            className="input"
            type="tel"
            value={typedNumber}
            onChange={(event) => setTypedNumber(event.target.value)}
            placeholder="(503) 555-0147"
            autoComplete="off"
            disabled={pending}
          />
        </label>
      )}

      <label className="field">
        <span className="field__label">Name</span>
        <input
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={NAME_MAX}
          autoComplete="off"
          disabled={pending}
        />
      </label>

      <label className="field">
        <span className="field__label">Note</span>
        <textarea
          className="input"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={NOTE_MAX}
          rows={3}
          disabled={pending}
        />
      </label>

      <fieldset className="field" disabled={pending}>
        <legend className="field__label">Standing</legend>
        <span className="field__choices">
          {STANDINGS.map((option) => (
            <label key={option.label} className="field__choice">
              <input
                type="radio"
                name="standing"
                checked={standing === option.value}
                onChange={() => setStanding(option.value)}
              />
              {option.label}
            </label>
          ))}
        </span>
        {standing === 'blocked' && (
          <p className="field__hint">
            Future calls are turned away before they ring. A call from them in the queue now ends.
          </p>
        )}
      </fieldset>

      {error && (
        <p className="dialog__error" role="alert">
          {error}
        </p>
      )}

      <div className="dialog__actions">
        {contact && (
          <button
            type="button"
            className="button button--ghost button--compact dialog__remove"
            onClick={() => onRemove(contact.number)}
            disabled={pending}
          >
            Remove contact
          </button>
        )}
        <button
          type="button"
          className="button button--ghost button--compact"
          onClick={onCancel}
          disabled={pending}
        >
          Cancel
        </button>
        <button type="submit" className="button button--accept button--compact" disabled={!canSave}>
          {pending ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  );
}

/** Blank means cleared: the server stores null, never an empty string. */
function changedFields(
  contact: Contact | null,
  name: string,
  note: string,
  standing: ContactStanding | null,
): ContactPatch {
  const patch: ContactPatch = {};
  const nextName = name.trim() || null;
  const nextNote = note.trim() || null;
  if (nextName !== (contact?.name ?? null)) patch.name = nextName;
  if (nextNote !== (contact?.note ?? null)) patch.note = nextNote;
  if (standing !== (contact?.standing ?? null)) patch.standing = standing;
  return patch;
}
